import logging
import time
from dataclasses import dataclass
from typing import Iterable

import paramiko
from flask import current_app

logger = logging.getLogger(__name__)

# Allowlist of permitted command prefixes.
# Only commands whose stripped text starts with one of these prefixes may be
# executed on a remote host. Extend this list deliberately — never use a
# catch-all wildcard.
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_allowed(self, command: str) -> None:
        stripped = command.strip()
        if not any(stripped.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES):
            raise CommandNotAllowedError(f"Command not in SSH allowlist: {stripped!r}")

    def _connect(self, host: str) -> paramiko.SSHClient:
        if not self.ssh_key_path:
            raise ValueError("ORBITAL_SSH_KEY_PATH must be configured for live SSH execution")

        client = paramiko.SSHClient()
        # Newly provisioned servers have no prior known_hosts entry.
        # AutoAddPolicy accepts and persists the key on first connection;
        # the fingerprint is logged so any unexpected change is auditable.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            port=self.SSH_PORT,
            username=self.ssh_user,
            key_filename=self.ssh_key_path,
            timeout=self.CONNECT_TIMEOUT,
            look_for_keys=False,
            allow_agent=False,
        )
        host_key = client.get_transport().get_remote_server_key()
        logger.info("SSH connected to %s — host key %s %s", host, host_key.get_name(), host_key.get_base64())
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
