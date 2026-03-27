import socket
from dataclasses import dataclass

from app.services.hetzner import HetznerClient
from app.services.ssh import SSHExecutor
from app.services.templating import DeploymentTemplateService, RenderedDeploymentFiles


@dataclass
class PipelineContext:
    project_name: str
    slug: str
    framework: str
    domain: str | None
    repository_url: str | None = None
    repository_branch: str = "main"
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

    def render_files(self, ctx: PipelineContext):
        return self.templates.render(
            framework=ctx.framework,
            app_name=ctx.slug,
            domain=ctx.domain,
            app_port=ctx.app_port,
        )

    def upload_and_deploy(self, host: str, ctx: PipelineContext, rendered: RenderedDeploymentFiles):
        if not ctx.repository_url:
            raise ValueError("Repository URL fehlt. Bitte im Projekt eine Repository URL konfigurieren.")

        branch = (ctx.repository_branch or "main").strip() or "main"
        deploy_dir = f"/opt/orbital/{ctx.slug}"
        commands = [
            f"rm -rf {deploy_dir}",
            f"git clone --branch {branch} {ctx.repository_url} {deploy_dir}",
            f"cat <<'EOF' > {deploy_dir}/docker-compose.yml\n{rendered.compose}EOF",
            f"cat <<'EOF' > {deploy_dir}/Dockerfile\n{rendered.dockerfile}EOF",
            f"cat <<'EOF' > {deploy_dir}/nginx.conf\n{rendered.nginx_conf}EOF",
            f"cat <<'EOF' > {deploy_dir}/.env\nDEBUG=False\nEOF",
            f"test -f {deploy_dir}/docker-compose.yml",
            f"docker-compose -f {deploy_dir}/docker-compose.yml down --remove-orphans",
            f"docker-compose -f {deploy_dir}/docker-compose.yml up -d --build --force-recreate",
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

    def healthcheck(self, host: str, ctx: PipelineContext):
        commands = [f"curl -s http://127.0.0.1:{ctx.app_port} | head -c 50"]
        return self.ssh.run_many(host, commands)
