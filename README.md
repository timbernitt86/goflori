# Orbital Starter

A pragmatic starter backend for **Orbital**: an AI-assisted infrastructure operator for deploying apps to Hetzner.

This is **not** the full product yet. It is the first production-minded scaffold with:

- Flask API
- SQLAlchemy models
- Celery worker integration
- project + deployment lifecycle
- provider abstraction for Hetzner
- deterministic deployment pipeline skeleton
- repo analysis + template generation placeholders

## Product scope for v1

Golden path:

1. Create a project
2. Attach repository metadata
3. Create a deployment
4. Run deterministic steps:
   - create server
   - prepare host
   - render deployment files
   - upload files
   - configure reverse proxy
   - issue SSL
   - run healthcheck

The AI layer should only help with:

- repo analysis
- template selection
- config suggestions
- log summarization
- fix suggestions

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
flask --app run.py db init
flask --app run.py db migrate -m "initial schema"
flask --app run.py db upgrade
python run.py
```

In another shell, for async jobs:

```bash
source .venv/bin/activate
celery -A worker.celery_app worker --loglevel=info
```

## API overview

### Health

- `GET /health`

### Projects

- `GET /api/projects`
- `POST /api/projects`
- `GET /api/projects/<project_id>`

### Deployments

- `POST /api/projects/<project_id>/deployments`
- `GET /api/deployments/<deployment_id>`
- `POST /api/deployments/<deployment_id>/run`

## Suggested next build steps

1. Add user auth
2. Add encrypted secret storage (Vault/KMS or app-level envelope encryption)
3. Replace local file rendering with real repo clone + analysis
4. Implement Hetzner API calls
5. Implement SSH executor with strict allowlisted commands
6. Persist step logs to object storage
7. Add frontend dashboard
8. Add audit trails and approvals for destructive actions

## Notes

- The current Hetzner provider is intentionally a stub with TODOs.
- The current executor supports a dry-run mode so you can wire the pipeline before touching real infrastructure.
- SQLite works for local dev; use Postgres in real environments.
