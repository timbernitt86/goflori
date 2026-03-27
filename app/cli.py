from flask import current_app
from flask_migrate import upgrade


def register_cli(app):
    @app.cli.command("db-upgrade")
    def db_upgrade():
        """Apply pending Alembic migrations to the configured database."""
        with app.app_context():
            upgrade()
            current_app.logger.info("Database migrations applied")
