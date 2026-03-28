import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import paramiko
from flask import current_app, has_app_context

logger = logging.getLogger(__name__)

# Allowlist of permitted command prefixes.
# Only commands whose stripped text starts with one of these prefixes may be
# executed on a remote host. Extend this list deliberately and avoid wildcards.
ALLOWED_COMMAND_PREFIXES: tuple[str, ...] = (
    "apt-get update",
    "apt-get install",
    "git clone",
    "systemctl enable",
    "systemctl start",
    "systemctl reload",
    "systemctl restart",
    "systemctl stop",
    "mkdir -p /opt/orbital/",
    "rm -rf /opt/orbital/",
    "cd /opt/orbital/",
    "cat <<'EOF' > /opt/orbital/",
    "touch /opt/orbital/",
    "test -f /opt/orbital/",
    "ln -sf /opt/orbital/",
    "rm -f /etc/nginx/sites-enabled/default",
    "rm -f /etc/nginx/sites-enabled/",
    "nginx -t",
    "certbot --nginx",
    "curl -s http://127.0.0.1:",
    "curl -sS -I https://",
    "docker-compose -f /opt/orbital/",
    "docker-compose up",
    "docker-compose down",
    "docker-compose restart",
    "docker-compose pull",
    "docker ps",
    "docker images",
    "docker logs",
)


@dataclass
class CommandResult:
    command: str
    return_code: int
    stdout: str
    stderr: str


class CommandNotAllowedError(ValueError):
    """Raised when a command is not present in the SSH allowlist."""


class SSHWaitTimeoutError(TimeoutError):
    def __init__(self, message: str, attempts_log: list[str]):
        super().__init__(message)
        self.attempts_log = attempts_log


class SSHExecutor:
    SSH_PORT: int = 22
    CONNECT_TIMEOUT: int = 30   # seconds
    COMMAND_TIMEOUT: int = 300  # seconds per command

    def __init__(self):
        self.dry_run = current_app.config.get("ORBITAL_DRY_RUN", True)
        self.ssh_key_path: str = current_app.config.get("ORBITAL_SSH_KEY_PATH", "")
        self.ssh_user: str = current_app.config.get("ORBITAL_SSH_USER", "root")
        self._key_material: str = current_app.config.get("ORBITAL_SSH_PRIVATE_KEY", "") or self._db_private_key()
        self._generated_key_path: str | None = None

    @staticmethod
    def _db_private_key() -> str:
        """Return private key stored in Hetzner ProviderSetting, if any."""
        if not has_app_context():
            return ""
        try:
            from app.models import ProviderSetting
            setting = ProviderSetting.query.filter_by(provider_name="hetzner").first()
            return (setting.ssh_private_key if setting else "") or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_allowed(self, command: str) -> None:
        stripped = command.strip()
        if not any(stripped.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES):
            raise CommandNotAllowedError(f"Command not in SSH allowlist: {stripped!r}")

    def _ensure_embedded_key_file(self) -> str | None:
        if self._generated_key_path:
            return self._generated_key_path

        key_material = (self._key_material or "").strip()
        if not key_material:
            return None

        key_material = key_material.replace("\\n", "\n")
        key_path = os.path.join(tempfile.gettempdir(), "orbital_ssh_key.pem")
        with open(key_path, "w", encoding="utf-8") as handle:
            handle.write(key_material)
            if not key_material.endswith("\n"):
                handle.write("\n")

        try:
            os.chmod(key_path, 0o600)
        except OSError:
            # Best effort on platforms where chmod is limited.
            pass

        self._generated_key_path = key_path
        return self._generated_key_path

    def _resolve_key_path(self) -> str:
        if self.ssh_key_path:
            return self.ssh_key_path

        generated = self._ensure_embedded_key_file()
        if generated:
            self.ssh_key_path = generated
            return self.ssh_key_path

        raise RuntimeError(
            "Live SSH requires ORBITAL_SSH_KEY_PATH or ORBITAL_SSH_PRIVATE_KEY. "
            "Alternatively enable ORBITAL_DRY_RUN=true for local simulation."
        )

    def _connect(self, host: str) -> paramiko.SSHClient:
        key_path = self._resolve_key_path()

        client = paramiko.SSHClient()
        # Newly provisioned servers have no prior known_hosts entry.
        # AutoAddPolicy accepts and persists the key on first connection.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            port=self.SSH_PORT,
            username=self.ssh_user,
            key_filename=key_path,
            timeout=self.CONNECT_TIMEOUT,
            look_for_keys=False,
            allow_agent=False,
        )
        host_key = client.get_transport().get_remote_server_key()
        logger.info("SSH connected to %s; host key %s %s", host, host_key.get_name(), host_key.get_base64())
        return client

    def _exec(self, client: paramiko.SSHClient, command: str) -> CommandResult:
        _, stdout_fh, stderr_fh = client.exec_command(command, timeout=self.COMMAND_TIMEOUT, get_pty=False)
        stdout_data = stdout_fh.read().decode("utf-8", errors="replace")
        stderr_data = stderr_fh.read().decode("utf-8", errors="replace")
        return_code = stdout_fh.channel.recv_exit_status()
        logger.debug("SSH rc=%d cmd=%r stdout=%r stderr=%r", return_code, command, stdout_data[:200], stderr_data[:200])
        return CommandResult(command=command, return_code=return_code, stdout=stdout_data, stderr=stderr_data)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_one(self, host: str, command: str) -> CommandResult:
        """Execute a single command. Opens and closes its own connection."""
        if self.dry_run:
            return CommandResult(command=command, return_code=0, stdout=f"DRY RUN on {host}: {command}", stderr="")
        self._assert_allowed(command)
        client = self._connect(host)
        try:
            return self._exec(client, command)
        finally:
            client.close()

    def run_many(self, host: str, commands: Iterable[str]) -> list[CommandResult]:
        """Execute multiple commands over a single SSH connection."""
        command_list = list(commands)
        if self.dry_run:
            return [
                CommandResult(command=cmd, return_code=0, stdout=f"DRY RUN on {host}: {cmd}", stderr="")
                for cmd in command_list
            ]
        for cmd in command_list:
            self._assert_allowed(cmd)
        client = self._connect(host)
        try:
            return [self._exec(client, cmd) for cmd in command_list]
        finally:
            client.close()

    def upload_directory(
        self,
        host: str,
        local_dir: str,
        remote_dir: str,
        *,
        exclude_dir_names: set[str] | None = None,
        exclude_file_names: set[str] | None = None,
    ) -> None:
        if self.dry_run:
            logger.info("DRY RUN upload directory %s -> %s on %s", local_dir, remote_dir, host)
            return

        source = Path(local_dir)
        if not source.exists() or not source.is_dir():
            raise ValueError(f"Local directory does not exist: {local_dir}")

        excluded_dirs = exclude_dir_names or set()
        excluded_files = exclude_file_names or set()

        client = self._connect(host)
        try:
            sftp = client.open_sftp()
            try:
                self._mkdir_p_sftp(sftp, remote_dir)
                for root, dirs, files in os.walk(source):
                    dirs[:] = [dirname for dirname in dirs if dirname not in excluded_dirs]

                    rel_path = os.path.relpath(root, str(source))
                    rel_path = "" if rel_path == "." else rel_path.replace("\\", "/")
                    current_remote = remote_dir if not rel_path else f"{remote_dir}/{rel_path}"
                    self._mkdir_p_sftp(sftp, current_remote)

                    for dirname in dirs:
                        self._mkdir_p_sftp(sftp, f"{current_remote}/{dirname}")

                    for filename in files:
                        if filename in excluded_files:
                            continue
                        local_file = os.path.join(root, filename)
                        remote_file = f"{current_remote}/{filename}"
                        sftp.put(local_file, remote_file)
            finally:
                sftp.close()
        finally:
            client.close()

    def upload_text(self, host: str, remote_path: str, content: str) -> None:
        """Upload a text file via SFTP without embedding file content into shell commands."""
        if self.dry_run:
            logger.info("DRY RUN upload text file -> %s on %s", remote_path, host)
            return

        client = self._connect(host)
        try:
            sftp = client.open_sftp()
            try:
                parent = str(Path(remote_path).parent).replace("\\", "/")
                if parent and parent != ".":
                    self._mkdir_p_sftp(sftp, parent)
                with sftp.file(remote_path, "w") as remote_file:
                    remote_file.write(content)
            finally:
                sftp.close()
        finally:
            client.close()

    def _mkdir_p_sftp(self, sftp: paramiko.SFTPClient, remote_path: str) -> None:
        parts = [part for part in remote_path.split("/") if part]
        if remote_path.startswith("/"):
            current = "/"
        else:
            current = ""

        for part in parts:
            if current in {"", "/"}:
                next_path = f"/{part}" if current == "/" else part
            else:
                next_path = f"{current}/{part}"
            try:
                sftp.stat(next_path)
            except FileNotFoundError:
                sftp.mkdir(next_path)
            current = next_path

    def wait_until_reachable(
        self,
        host: str,
        *,
        max_attempts: int = 20,
        delay_seconds: int = 10,
    ) -> list[str]:
        if not host:
            raise ValueError("Server-IP fehlt fuer SSH-Erreichbarkeitspruefung")

        if self.dry_run:
            return [f"attempt=1 status=success host={host} mode=dry-run"]

        attempts_log: list[str] = []
        last_error: str | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                client = self._connect(host)
            except (paramiko.AuthenticationException, paramiko.SSHException, OSError, TimeoutError) as exc:
                last_error = str(exc)
                log_line = f"attempt={attempt} status=retry host={host} error={last_error}"
                attempts_log.append(log_line)
                logger.warning("SSH wait attempt %s/%s failed for %s: %s", attempt, max_attempts, host, exc)
                if attempt < max_attempts:
                    time.sleep(delay_seconds)
                continue

            client.close()
            log_line = f"attempt={attempt} status=success host={host}"
            attempts_log.append(log_line)
            logger.info("SSH became reachable on attempt %s/%s for %s", attempt, max_attempts, host)
            return attempts_log

        raise SSHWaitTimeoutError(
            f"Server {host} war nach {max_attempts} Versuchen und {delay_seconds}s Pause nicht per SSH erreichbar."
            + (f" Letzter Fehler: {last_error}" if last_error else ""),
            attempts_log,
        )
