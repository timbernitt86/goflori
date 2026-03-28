import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///orbital.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    _REDIS_URL = (os.getenv("REDIS_URL", "") or "").strip()
    CELERY = {
        "broker_url": _REDIS_URL or "memory://",
        "result_backend": _REDIS_URL or "cache+memory://",
        "task_ignore_result": not bool(_REDIS_URL),
    }
    ORBITAL_APP_NAME = os.getenv("ORBITAL_APP_NAME", "Orbital")
    ORBITAL_ENV = os.getenv("ORBITAL_ENV", "local")
    ORBITAL_DRY_RUN = os.getenv("ORBITAL_DRY_RUN", "false").lower() == "true"
    ORBITAL_AUTO_DB_UPGRADE = os.getenv("ORBITAL_AUTO_DB_UPGRADE", "true").lower() == "true"
    ORBITAL_INLINE_DEPLOY_ON_QUEUE_ERROR = os.getenv("ORBITAL_INLINE_DEPLOY_ON_QUEUE_ERROR", "true").lower() == "true"
    HETZNER_API_TOKEN = os.getenv("HETZNER_API_TOKEN", "")
    ORBITAL_SSH_KEY_PATH = os.getenv("ORBITAL_SSH_KEY_PATH", "")
    ORBITAL_SSH_PRIVATE_KEY = os.getenv("ORBITAL_SSH_PRIVATE_KEY", "")
    ORBITAL_SSH_USER = os.getenv("ORBITAL_SSH_USER", "root")
    ORBITAL_REPO_CLONE_ROOT = os.getenv("ORBITAL_REPO_CLONE_ROOT", "")
