from flask import Flask, redirect, url_for
from flask_migrate import upgrade

from app.config import Config
from app.extensions import db, migrate
from app.api import register_blueprints
from app.dashboard import bp as dashboard_bp
from app.tasks import init_celery


def _maybe_upgrade_database(app: Flask) -> None:
    auto_upgrade = app.config.get("ORBITAL_AUTO_DB_UPGRADE", True)
    if not auto_upgrade:
        return

    with app.app_context():
        try:
            upgrade()
            app.logger.info("Database migrations applied successfully during startup.")
        except Exception as exc:
            # Keep app booting, but make schema issues visible in logs.
            app.logger.exception("Database auto-upgrade failed during startup: %s", exc)
            try:
                db.create_all()
                app.logger.warning("Fallback db.create_all() executed after migration failure.")
            except Exception as bootstrap_exc:
                app.logger.exception("Fallback db.create_all() also failed: %s", bootstrap_exc)


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

    _maybe_upgrade_database(app)

    @app.get("/")
    def index():
        return redirect(url_for("dashboard.projects"))

    return app
