from .health import bp as health_bp
from .projects import bp as projects_bp
from .deployments import bp as deployments_bp


def register_blueprints(app):
    app.register_blueprint(health_bp)
    app.register_blueprint(projects_bp, url_prefix="/api")
    app.register_blueprint(deployments_bp, url_prefix="/api")
