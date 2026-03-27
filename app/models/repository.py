from app.extensions import db
from app.models.base import TimestampMixin


class Repository(TimestampMixin, db.Model):
    __tablename__ = "repositories"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, unique=True)
    provider = db.Column(db.String(50), nullable=False, default="github")
    url = db.Column(db.String(1000), nullable=False)
    branch = db.Column(db.String(255), nullable=False, default="main")

    project = db.relationship("Project", back_populates="repository")

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "provider": self.provider,
            "url": self.url,
            "branch": self.branch,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
