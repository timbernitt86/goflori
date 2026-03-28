from dataclasses import dataclass
from pathlib import Path


@dataclass
class RenderedDeploymentFiles:
    dockerfile: str
    compose: str
    nginx_conf: str
    metadata: dict

    def to_dict(self) -> dict:
        return {
            "dockerfile": self.dockerfile,
            "compose": self.compose,
            "nginx_conf": self.nginx_conf,
            "metadata": self.metadata,
        }


class DeploymentTemplateService:
    def _normalize_public_domain(self, domain: str | None) -> str | None:
        value = (domain or "").strip().lower()
        if not value:
            return None
        if value in {"localhost", "127.0.0.1", "0.0.0.0"}:
            return None
        return value

    def _resolve_flask_gunicorn_target(self, repository_path: Path | None) -> str:
        if repository_path is None:
            return "app:app"

        run_py = repository_path / "run.py"
        app_py = repository_path / "app.py"
        wsgi_py = repository_path / "wsgi.py"

        if run_py.exists():
            return "run:app"
        if app_py.exists():
            return "app:app"
        if wsgi_py.exists():
            content = wsgi_py.read_text(encoding="utf-8", errors="ignore")
            if "application" in content:
                return "wsgi:application"
            return "wsgi:app"
        return "app:app"

    def render(
        self,
        *,
        framework: str,
        app_name: str,
        domain: str | None,
        app_port: int = 8000,
        host_port: int | None = None,
        local_repository_path: str | None = None,
        build_source_dir: str = "repo",
    ) -> RenderedDeploymentFiles:
        framework = framework or "flask"
        public_domain = self._normalize_public_domain(domain)
        public_port = host_port or app_port
        repository_path = Path(local_repository_path) if local_repository_path else None
        requirements_exists = bool(repository_path and (repository_path / "requirements.txt").exists())
        gunicorn_target = self._resolve_flask_gunicorn_target(repository_path) if framework == "flask" else None

        if framework == "node":
            dockerfile = f"""FROM node:20-alpine
WORKDIR /app
COPY {build_source_dir}/package*.json ./
RUN npm install
COPY {build_source_dir}/ .
EXPOSE {app_port}
CMD ["npm", "start"]
"""
        elif framework == "laravel":
            dockerfile = f"""FROM php:8.3-cli
WORKDIR /app
COPY {build_source_dir}/ .
EXPOSE {app_port}
CMD ["sh", "-c", "php artisan serve --host=0.0.0.0 --port={app_port} || php -S 0.0.0.0:{app_port} -t public || php -S 0.0.0.0:{app_port}"]
"""
        else:
            dockerfile = f"""FROM python:3.12-slim
WORKDIR /app
COPY {build_source_dir}/ .
RUN pip install --no-cache-dir gunicorn flask 2>/dev/null; if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi || true
EXPOSE {app_port}
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:{app_port} {gunicorn_target} --access-logfile - --error-logfile - || python -m flask run --host=0.0.0.0 --port={app_port} || python -m http.server {app_port}"]
"""

        compose = f"""services:
  web:
    build: .
    container_name: {app_name}-web
    restart: unless-stopped
    ports:
            - "127.0.0.1:{public_port}:{app_port}"
    # Runtime ENV values are injected into this file during upload_artifacts.
    env_file:
      - .env
"""

        server_name = public_domain or "_"
        nginx_conf = f"""server {{
    listen 80;
    server_name {server_name};

    location / {{
            proxy_pass http://127.0.0.1:{public_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""

        return RenderedDeploymentFiles(
            dockerfile=dockerfile,
            compose=compose,
            nginx_conf=nginx_conf,
            metadata={
                "framework": framework,
                "project_name": app_name,
                "app_port": app_port,
                "host_port": public_port,
                "public_domain": public_domain,
                "local_repository_path": local_repository_path,
                "build_context": local_repository_path,
                "build_source_dir": build_source_dir,
                "gunicorn_command": f"gunicorn -b 0.0.0.0:{app_port} {gunicorn_target}" if gunicorn_target else None,
                "requirements_txt_found": requirements_exists,
                "generated_files": ["Dockerfile", "docker-compose.yml", "nginx.conf"],
            },
        )
