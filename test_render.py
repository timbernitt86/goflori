from app import create_app
from app.services.execution import DeploymentExecutor, PipelineContext
from app.services.templating import DeploymentTemplateService

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
    
    svc = DeploymentTemplateService()
    rendered = svc.render(framework='flask', app_name='test-app', domain='example.com', app_port=8000)
    
    print('Template output analysis:')
    print('=' * 70)
    print(f'compose ends with newline: {rendered.compose.endswith(chr(10))}')
    print(f'compose last 20 chars: {repr(rendered.compose[-20:])}')
    print()
    print(f'dockerfile ends with newline: {rendered.dockerfile.endswith(chr(10))}')
    print(f'dockerfile last 20 chars: {repr(rendered.dockerfile[-20:])}')
    print()
    print(f'nginx_conf ends with newline: {rendered.nginx_conf.endswith(chr(10))}')
    print(f'nginx_conf last 20 chars: {repr(rendered.nginx_conf[-20:])}')
