from app import create_app
from app.tasks import init_celery

flask_app = create_app()
celery_app = init_celery(flask_app)
