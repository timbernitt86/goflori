from app.extensions import db
from app.models.base import TimestampMixin


class Repository(TimestampMixin, db.Model):
    __tablename__ = "repositories"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, unique=True)
    provider = db.Column(db.String(50), nullable=True)
    repo_url = db.Column(db.String(1000), nullable=False)
    branch = db.Column(db.String(255), nullable=False, default="main")
    access_token = db.Column(db.String(512), nullable=True)
    is_private = db.Column(db.Boolean, nullable=False, default=False)

    project = db.relationship("Project", back_populates="repository")

    # Backward-compatible alias for older code paths still using "url".
    @property
    def url(self) -> str:
        return self.repo_url

    @url.setter
    def url(self, value: str) -> None:
        self.repo_url = value

    @property
    def has_access_token(self) -> bool:
        return bool((self.access_token or "").strip())

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "provider": self.provider,
            "repo_url": self.repo_url,
            "url": self.repo_url,
            "branch": self.branch,
            "is_private": self.is_private,
            "has_access_token": self.has_access_token,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
