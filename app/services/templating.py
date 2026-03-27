from dataclasses import dataclass


@dataclass
class RenderedDeploymentFiles:
    dockerfile: str
    compose: str
    nginx_conf: str


class DeploymentTemplateService:
    def render(self, *, framework: str, app_name: str, domain: str | None, app_port: int = 8000) -> RenderedDeploymentFiles:
        framework = framework or "flask"

        if framework == "node":
            dockerfile = f"""FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
EXPOSE {app_port}
CMD ["npm", "start"]
"""
        else:
            dockerfile = f"""FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir gunicorn flask 2>/dev/null; if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi || true
EXPOSE {app_port}
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:{app_port} app:app 2>/dev/null || python -m flask run --host=0.0.0.0 --port={app_port} 2>/dev/null || python -m http.server {app_port}"]
"""

        compose = f"""services:
  web:
    build: .
    container_name: {app_name}-web
    restart: unless-stopped
    ports:
      - "127.0.0.1:{app_port}:{app_port}"
    env_file:
      - .env
"""

        server_name = domain or "_"
        nginx_conf = f"""server {{
    listen 80;
    server_name {server_name};

    location / {{
        proxy_pass http://127.0.0.1:{app_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""

        return RenderedDeploymentFiles(dockerfile=dockerfile, compose=compose, nginx_conf=nginx_conf)
