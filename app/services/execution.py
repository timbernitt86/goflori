import socket
from dataclasses import dataclass, field

from app.services.hetzner import HetznerClient
from app.services.ssh import CommandResult, SSHExecutor
from app.services.templating import DeploymentTemplateService, RenderedDeploymentFiles


@dataclass
class PipelineContext:
    project_name: str
    slug: str
    framework: str
    domain: str | None
    repository_url: str | None = None
    repository_branch: str = "main"
    local_repository_path: str | None = None
    deployment_mode: str = "repository"
    project_environment: str = "production"
    env_values: dict[str, str] = field(default_factory=dict)
    env_secret_keys: set[str] = field(default_factory=set)
    server_type: str = "cx22"
    region: str = "nbg1"
    app_port: int = 8000


@dataclass
class DNSCheckResult:
    resolved_ip: str
    expected_ip: str
    matches: bool


class DeploymentExecutor:
    def __init__(self):
        self.hetzner = HetznerClient()
        self.ssh = SSHExecutor()
        self.templates = DeploymentTemplateService()

    def create_server(self, ctx: PipelineContext):
        return self.hetzner.create_server(name=f"orbital-{ctx.slug}", server_type=ctx.server_type, region=ctx.region)

    def wait_for_ssh(self, host: str, *, max_attempts: int = 20, delay_seconds: int = 10):
        return self.ssh.wait_until_reachable(host, max_attempts=max_attempts, delay_seconds=delay_seconds)

    def prepare_host(self, host: str):
        commands = [
            "apt-get update -y",
            "apt-get install -y git docker.io docker-compose nginx certbot python3-certbot-nginx",
            "systemctl enable docker",
            "systemctl start docker",
        ]
        return self.ssh.run_many(host, commands)

    def render_artifacts_from_repo(self, ctx: PipelineContext):
        return self.templates.render(
            framework=ctx.framework,
            app_name=ctx.slug,
            domain=ctx.domain,
            app_port=ctx.app_port,
            local_repository_path=ctx.local_repository_path,
            build_source_dir="repo",
        )

    def _render_env_file(self, ctx: PipelineContext) -> tuple[str, dict]:
        """Build .env content from project variables.

        Project variables from the dashboard are the source of truth. We add
        a small set of defaults if they are not explicitly configured.
        """
        merged: dict[str, str] = dict(ctx.env_values)
        merged.setdefault("ORBITAL_PROJECT_NAME", ctx.project_name)
        merged.setdefault("ORBITAL_PROJECT_SLUG", ctx.slug)
        merged.setdefault("ORBITAL_ENV", ctx.project_environment)
        merged.setdefault("PORT", str(ctx.app_port))

        lines = [f"{key}={value}" for key, value in sorted(merged.items())]
        content = "\n".join(lines) + "\n"
        meta = {
            "env_key_count": len(merged),
            "env_keys": sorted(merged.keys()),
            "secret_key_count": len(ctx.env_secret_keys),
            "secret_keys": sorted(ctx.env_secret_keys),
        }
        return content, meta

    def upload_artifacts(self, host: str, ctx: PipelineContext, rendered: RenderedDeploymentFiles):
        deploy_dir = f"/opt/orbital/{ctx.slug}"
        repo_snapshot_dir = f"{deploy_dir}/repo"
        results: list[CommandResult] = []
        results.append(self.ssh.run_one(host, f"rm -rf {deploy_dir}"))
        results.append(self.ssh.run_one(host, f"mkdir -p {deploy_dir}"))
        results.append(self.ssh.run_one(host, f"mkdir -p {repo_snapshot_dir}"))

        if ctx.local_repository_path:
            self.ssh.upload_directory(
                host,
                ctx.local_repository_path,
                repo_snapshot_dir,
                exclude_dir_names={
                    ".git",
                    ".venv",
                    "venv",
                    "node_modules",
                    "__pycache__",
                    ".pytest_cache",
                    ".mypy_cache",
                    ".ruff_cache",
                    ".tox",
                    ".idea",
                    ".vscode",
                },
                exclude_file_names={
                    ".DS_Store",
                },
            )
            results.append(
                CommandResult(
                    command=f"upload_directory {ctx.local_repository_path} -> {repo_snapshot_dir}",
                    return_code=0,
                    stdout="Repository snapshot uploaded successfully",
                    stderr="",
                )
            )
        else:
            # Fallback-App NUR im repo/-Verzeichnis erzeugen, niemals in /app oder anderen Verzeichnissen!
            if repo_snapshot_dir.rstrip("/").endswith("/repo"):
                fallback_files = [
                    f"cat <<'EOF' > {repo_snapshot_dir}/app.py\nfrom flask import Flask\napp = Flask(__name__)\n\n@app.get('/')\ndef index():\n    return 'Orbital fallback app is running'\nEOF",
                    f"cat <<'EOF' > {repo_snapshot_dir}/requirements.txt\nflask\ngunicorn\nEOF",
                ]
                fallback_results = self.ssh.run_many(host, fallback_files)
                results.extend(fallback_results)
            else:
                # Niemals Fallback-App außerhalb von repo/-Verzeichnis erzeugen!
                results.append(CommandResult(
                    command=f"SKIP fallback app creation: repo_snapshot_dir={repo_snapshot_dir}",
                    return_code=0,
                    stdout="Fallback app creation skipped (invalid target dir)",
                    stderr="",
                ))

        env_content, env_meta = self._render_env_file(ctx)
        self.ssh.upload_text(host, f"{deploy_dir}/docker-compose.yml", rendered.compose)
        self.ssh.upload_text(host, f"{deploy_dir}/Dockerfile", rendered.dockerfile)
        self.ssh.upload_text(host, f"{deploy_dir}/nginx.conf", rendered.nginx_conf)
        self.ssh.upload_text(host, f"{deploy_dir}/.env", env_content)

        results.append(CommandResult(command=f"upload_text {deploy_dir}/docker-compose.yml", return_code=0, stdout="docker-compose artifact uploaded", stderr=""))
        results.append(CommandResult(command=f"upload_text {deploy_dir}/Dockerfile", return_code=0, stdout="Dockerfile artifact uploaded", stderr=""))
        results.append(CommandResult(command=f"upload_text {deploy_dir}/nginx.conf", return_code=0, stdout="nginx config artifact uploaded", stderr=""))
        results.append(
            CommandResult(
                command=f"upload_text {deploy_dir}/.env",
                return_code=0,
                stdout=(
                    f"env artifact uploaded (keys={env_meta['env_key_count']}, "
                    f"secret_keys={env_meta['secret_key_count']})"
                ),
                stderr="",
            )
        )

        commands = [
            f"test -f {deploy_dir}/docker-compose.yml",
            f"test -f {deploy_dir}/Dockerfile",
            f"test -f {deploy_dir}/nginx.conf",
            f"test -f {deploy_dir}/.env",
        ]
        results.extend(self.ssh.run_many(host, commands))
        return results

    def start_containers(self, host: str, ctx: PipelineContext):
        deploy_dir = f"/opt/orbital/{ctx.slug}"
        migrate_command = (
            "docker-compose -f "
            f"{deploy_dir}/docker-compose.yml exec -T web sh -lc "
            "'flask --app run db-upgrade "
            "|| flask --app run.py db-upgrade "
            "|| flask --app app db upgrade "
            "|| flask --app app.py db upgrade "
            "|| echo \"Skipping DB migration: no compatible Flask app/command found\"'"
        )
        commands = [
            f"docker-compose -f {deploy_dir}/docker-compose.yml down --remove-orphans",
            f"docker-compose -f {deploy_dir}/docker-compose.yml up -d --build --force-recreate",
            migrate_command,
        ]
        return self.ssh.run_many(host, commands)

    def configure_reverse_proxy(self, host: str, ctx: PipelineContext):
        commands = [
            f"test -f /opt/orbital/{ctx.slug}/nginx.conf",
            f"ln -sf /opt/orbital/{ctx.slug}/nginx.conf /etc/nginx/sites-enabled/{ctx.slug}.conf",
            "rm -f /etc/nginx/sites-enabled/default",
            "nginx -t",
            "systemctl reload nginx",
        ]
        return self.ssh.run_many(host, commands)

    def check_dns(self, domain: str | None, expected_ip: str | None) -> DNSCheckResult:
        expected = (expected_ip or "").strip()
        if not domain:
            return DNSCheckResult(resolved_ip="-", expected_ip=expected or "-", matches=False)

        if not expected:
            return DNSCheckResult(resolved_ip="-", expected_ip="-", matches=False)

        try:
            addrinfo = socket.getaddrinfo(domain, None, socket.AF_INET)
            resolved_ips = sorted({entry[4][0] for entry in addrinfo if entry and entry[4]})
        except socket.gaierror:
            resolved_ips = []

        resolved_ip = resolved_ips[0] if resolved_ips else "-"
        return DNSCheckResult(
            resolved_ip=resolved_ip,
            expected_ip=expected,
            matches=expected in resolved_ips,
        )

    def run_certbot(self, host: str, domain: str | None):
        if not domain:
            return []
        commands = [f"certbot --nginx -d {domain} --non-interactive --agree-tos -m admin@{domain}"]
        return self.ssh.run_many(host, commands)

    def verify_https(self, host: str, domain: str | None):
        if not domain:
            return []
        commands = [f"curl -sS -I https://{domain}"]
        return self.ssh.run_many(host, commands)

    def cleanup_project_from_server(self, host: str, ctx: PipelineContext):
        deploy_dir = f"/opt/orbital/{ctx.slug}"
        commands = [
            f"docker-compose -f {deploy_dir}/docker-compose.yml down --volumes --remove-orphans || true",
            f"rm -f /etc/nginx/sites-enabled/{ctx.slug}.conf",
            "nginx -t",
            "systemctl reload nginx",
            f"rm -rf {deploy_dir}",
        ]
        return self.ssh.run_many(host, commands)

    def healthcheck(self, host: str, ctx: PipelineContext):
        commands = [f"curl -s http://127.0.0.1:{ctx.app_port} | head -c 50"]
        return self.ssh.run_many(host, commands)
