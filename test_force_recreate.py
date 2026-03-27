from app import create_app
from app.services.ssh import ALLOWED_COMMAND_PREFIXES

app = create_app()

with app.app_context():
    test_cmd = 'docker-compose -f /opt/orbital/test-app/docker-compose.yml up -d --build --force-recreate'
    stripped = test_cmd.strip()
    allowed = any(stripped.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES)
    
    print(f'Command: {test_cmd}')
    print(f'Allowed: {allowed}')
    
    if allowed:
        print('Status: OK')
    else:
        print('Matching prefixes:')
        for prefix in ALLOWED_COMMAND_PREFIXES:
            if 'docker' in prefix:
                print(f'  - {prefix}')
