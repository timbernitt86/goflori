from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app import create_app
from app.config import Config
from app.extensions import db


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    ORBITAL_AUTO_DB_UPGRADE = False
    ORBITAL_DRY_RUN = True
    ORBITAL_INLINE_DEPLOY_ON_QUEUE_ERROR = False
    CELERY = {
        "broker_url": "memory://",
        "result_backend": "cache+memory://",
        "task_ignore_result": True,
    }


@dataclass
class JourneyContext:
    client: Any
    state: dict[str, Any] = field(default_factory=dict)


@pytest.fixture()
def app():
    app = create_app(TestingConfig)
    with app.app_context():
        db.drop_all()
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def journey_context(client):
    return JourneyContext(client=client)
