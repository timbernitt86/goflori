from flask import Flask, redirect, url_for

from app.config import Config
from app.extensions import db, migrate
from app.api import register_blueprints
from app.dashboard import bp as dashboard_bp
from app.tasks import init_celery


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    init_celery(app)
    register_blueprints(app)
    app.register_blueprint(dashboard_bp)

    # Import models so SQLAlchemy metadata is registered for Flask-Migrate.
    from app import models  # noqa: F401

    @app.get("/")
    def index():
        return redirect(url_for("dashboard.projects"))

    return app
