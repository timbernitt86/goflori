#!/usr/bin/env python
from app import create_app
from app.services.execution import DeploymentExecutor, PipelineContext
from app.services.ssh import ALLOWED_COMMAND_PREFIXES

app = create_app()

with app.app_context():
    ctx = PipelineContext(
        project_name='Test',
        slug='test-app',
        framework='flask',
        domain='example.com',
        repository_url='https://github.com/example/repo.git',
        repository_branch='main',
        app_port=8000
    )

    # Build commands list
    executor = DeploymentExecutor()
    rendered = executor.render_files(ctx)

    commands = [
        f'rm -rf /opt/orbital/{ctx.slug}',
        f'git clone --branch {ctx.repository_branch} {ctx.repository_url} /opt/orbital/{ctx.slug}',
        f'cat <<\'EOF\' > /opt/orbital/{ctx.slug}/docker-compose.yml\n{rendered.compose}EOF',
        f'cat <<\'EOF\' > /opt/orbital/{ctx.slug}/Dockerfile\n{rendered.dockerfile}EOF',
        f'cat <<\'EOF\' > /opt/orbital/{ctx.slug}/nginx.conf\n{rendered.nginx_conf}EOF',
        f'cat <<\'EOF\' > /opt/orbital/{ctx.slug}/.env\nDEBUG=False\nEOF',
        f'test -f /opt/orbital/{ctx.slug}/docker-compose.yml',
        f'docker-compose -f /opt/orbital/{ctx.slug}/docker-compose.yml down --remove-orphans',
        f'docker-compose -f /opt/orbital/{ctx.slug}/docker-compose.yml up -d --build --force-recreate',
    ]

    print('SSH COMMAND VALIDATION')
    print('=' * 70)
    all_valid = True
    for i, cmd in enumerate(commands, 1):
        stripped = cmd.strip()
        allowed = any(stripped.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES)
        status = 'OK' if allowed else 'FAIL'
        display = (stripped[:60] + '...') if len(stripped) > 60 else stripped
        print(f'[{status}] Cmd {i}: {display}')
        if not allowed:
            all_valid = False
            print(f'      ERROR: No matching allowlist prefix!')

    print('=' * 70)
    print(f'Result: {"ALL VALID" if all_valid else "VALIDATION FAILED"}')
