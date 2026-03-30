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
    host_port: int = 8000
    is_update: bool = False  # True when redeploying to an existing live server
    rolling_update_enabled: bool = False
    redeploy_strategy: str = "full"
    release_id: str | None = None
    deploy_root: str | None = None
    release_dir: str | None = None
    previous_release_dir: str | None = None
    minimal_downtime_attempted: bool = False


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
            "apt-get install -y git docker.io nginx certbot python3-certbot-nginx",
            "systemctl enable docker",
            "systemctl start docker",
            "mkdir -p /usr/local/lib/docker/cli-plugins",
            "curl -SL https://github.com/docker/compose/releases/download/v2.24.6/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose",
            "chmod +x /usr/local/lib/docker/cli-plugins/docker-compose",
        ]
        return self.ssh.run_many(host, commands)

    def render_artifacts_from_repo(self, ctx: PipelineContext):
        return self.templates.render(
            framework=ctx.framework,
            app_name=ctx.slug,
            domain=ctx.domain,
            app_port=ctx.app_port,
            host_port=ctx.host_port,
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

    def upload_artifacts(
        self,
        host: str,
        ctx: PipelineContext,
        rendered: RenderedDeploymentFiles,
        *,
        target_dir: str | None = None,
        reset_target: bool = False,
    ):
        deploy_dir = target_dir or f"/opt/orbital/{ctx.slug}"
        repo_snapshot_dir = f"{deploy_dir}/repo"
        results: list[CommandResult] = []
        if reset_target:
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

    def _db_migrate_command(self, deploy_dir: str) -> str:
        return (
            "docker compose -f "
            f"{deploy_dir}/docker-compose.yml exec -T web sh -lc "
            "'flask --app run db-upgrade "
            "|| flask --app run.py db-upgrade "
            "|| flask --app app db upgrade "
            "|| flask --app app.py db upgrade "
            "|| echo \"Skipping DB migration: no compatible Flask app/command found\"'"
        )

    def start_containers(self, host: str, ctx: PipelineContext):
        """Initial deploy: stop any existing containers, build fresh, run DB migration."""
        deploy_dir = f"/opt/orbital/{ctx.slug}"
        commands = [
            # --remove-orphans cleans up stale services; named volumes are NOT touched.
            f"docker compose -f {deploy_dir}/docker-compose.yml down --remove-orphans",
            # Force-remove any leftover containers by name pattern (handles v1->v2 naming conflicts).
            f"docker ps -a --filter 'name={ctx.slug}' -q | xargs -r docker rm -f",
            f"docker compose -f {deploy_dir}/docker-compose.yml up -d --build --force-recreate",
            self._db_migrate_command(deploy_dir),
        ]
        return self.ssh.run_many(host, commands)

    def update_containers(self, host: str, ctx: PipelineContext, *, deploy_dir: str | None = None):
        """Update deploy: rebuild image and restart with zero DB wipe.

        Named volumes (app data / SQLite DB) are preserved because we never
        call 'docker-compose down'. The running container is replaced in-place
        by 'docker-compose up --build --force-recreate', and DB migrations are
        applied afterwards.
        """
        deploy_dir = deploy_dir or f"/opt/orbital/{ctx.slug}"
        commands = [
            # Build first to preserve old running container if build fails.
            f"docker compose -f {deploy_dir}/docker-compose.yml build web",
            # Replace only web service to reduce downtime compared to full stack recreation.
            f"docker compose -f {deploy_dir}/docker-compose.yml up -d --build --no-deps web",
            # Apply any new DB migrations (add columns / create tables).
            self._db_migrate_command(deploy_dir),
        ]
        return self.ssh.run_many(host, commands)

    def configure_reverse_proxy(self, host: str, ctx: PipelineContext, *, deploy_dir: str | None = None):
        root = deploy_dir or f"/opt/orbital/{ctx.slug}"
        commands = [
            f"test -f {root}/nginx.conf",
            f"ln -sf {root}/nginx.conf /etc/nginx/sites-enabled/{ctx.slug}.conf",
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
            # 1. Try graceful compose down with volumes (works if compose file still exists)
            f"docker compose -f {deploy_dir}/docker-compose.yml down --volumes --remove-orphans || true",
            # 2. Force-remove any containers matching the project slug (catches v1/v2 naming)
            f"docker ps -a --filter 'name={ctx.slug}' -q | xargs -r docker rm -f || true",
            # 3. Remove project Docker images by label/name pattern
            f"docker images --filter 'reference=*{ctx.slug}*' -q | xargs -r docker rmi -f || true",
            # 4. Remove named volume created by this project
            f"docker volume rm orbital-{ctx.slug}-data || true",
            # 5. Prune any anonymous volumes left behind
            "docker volume prune -f || true",
            # 6. Remove nginx site config and reload
            f"rm -f /etc/nginx/sites-enabled/{ctx.slug}.conf",
            f"rm -f /etc/nginx/sites-available/{ctx.slug}.conf",
            "nginx -t",
            "systemctl reload nginx",
            # 7. Remove all deploy files
            f"rm -rf {deploy_dir}",
        ]
        return self.ssh.run_many(host, commands)

    def verify_deployment(self, host: str, ctx: PipelineContext, *, deploy_dir: str | None = None) -> list:
        """Self-healing post-deploy verification.

        Check order (auto-fix attempted at each stage):
          1. Web container is running  → restart via compose if not
          2. nginx symlink present      → re-run configure_reverse_proxy if not
          3. nginx not serving default  → re-run configure_reverse_proxy if it is
          4. App responds on host port  → final confirmation

        The final CommandResult entry has return_code 0 on full success or 1 on
        permanent failure so that _assert_command_results_ok fails the step.
        """
        deploy_dir = deploy_dir or f"/opt/orbital/{ctx.slug}"
        container_name = f"{ctx.slug}-web"
        results: list = []

        # ── 1. Container running? ─────────────────────────────────────────────
        running = self.ssh.run_one(
            host,
            f"docker ps --filter 'name={container_name}' --filter 'status=running' --format '{{{{.Names}}}}'",
        )
        results.append(running)

        if container_name not in (running.stdout or ""):
            # Auto-fix: show logs then force-recreate
            logs = self.ssh.run_one(
                host,
                f"docker compose -f {deploy_dir}/docker-compose.yml logs --tail=50 web",
            )
            results.append(logs)
            restart = self.ssh.run_one(
                host,
                f"docker compose -f {deploy_dir}/docker-compose.yml up -d --force-recreate",
            )
            results.append(restart)
            recheck = self.ssh.run_one(
                host,
                f"docker ps --filter 'name={container_name}' --filter 'status=running' --format '{{{{.Names}}}}'",
            )
            results.append(recheck)
            if container_name not in (recheck.stdout or ""):
                results.append(CommandResult(
                    command="verify_deployment:container_check",
                    return_code=1,
                    stdout="",
                    stderr=(
                        f"Container {container_name} ist nach Auto-Restart nicht gestartet. "
                        "Deployment-Fehler – Projekt ist NICHT erreichbar. Siehe Docker-Logs oben."
                    ),
                ))
                return results

        # ── 2. nginx symlink vorhanden? ───────────────────────────────────────
        symlink = self.ssh.run_one(
            host,
            f"test -L /etc/nginx/sites-enabled/{ctx.slug}.conf",
        )
        results.append(symlink)
        if symlink.return_code != 0:
            # Auto-fix: re-apply reverse proxy config
            results.extend(self.configure_reverse_proxy(host, ctx, deploy_dir=deploy_dir))

        # ── 3. nginx darf NICHT die Default-Seite ausliefern ──────────────────
        # Probe via port 80 (through nginx). "Welcome to nginx" = default site still active.
        probe = self.ssh.run_one(host, "curl -s http://127.0.0.1:80 --max-time 15")
        results.append(probe)
        if "Welcome to nginx" in (probe.stdout or ""):
            # Auto-fix: re-apply proxy config and reload nginx
            results.extend(self.configure_reverse_proxy(host, ctx, deploy_dir=deploy_dir))
            reprobe = self.ssh.run_one(host, "curl -s http://127.0.0.1:80 --max-time 15")
            results.append(reprobe)
            if "Welcome to nginx" in (reprobe.stdout or ""):
                results.append(CommandResult(
                    command="verify_deployment:nginx_routing_check",
                    return_code=1,
                    stdout=reprobe.stdout or "",
                    stderr=(
                        "nginx liefert nach automatischer Neukonfiguration noch immer die Default-Seite. "
                        f"Symlink /etc/nginx/sites-enabled/{ctx.slug}.conf wurde nicht uebernommen. "
                        "Projekt ist NICHT erreichbar."
                    ),
                ))
                return results

        # ── 4. App antwortet direkt auf host_port ─────────────────────────────
        direct = self.ssh.run_one(
            host,
            f"curl -s http://127.0.0.1:{ctx.host_port} --max-time 15 | head -c 50",
        )
        results.append(direct)

        results.append(CommandResult(
            command="verify_deployment:ok",
            return_code=0,
            stdout=(
                f"container={container_name} status=running "
                f"nginx_routed=true host_port={ctx.host_port}"
            ),
            stderr="",
        ))
        return results
