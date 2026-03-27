from app.extensions import db
from app.models.base import TimestampMixin


class EnvironmentVariable(TimestampMixin, db.Model):
    __tablename__ = "environment_variables"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    key = db.Column(db.String(255), nullable=False)
    value = db.Column(db.Text, nullable=False)
    is_secret = db.Column(db.Boolean, nullable=False, default=True)

    project = db.relationship("Project", back_populates="environment_variables")

    def to_dict(self, mask_secrets: bool = False):
        value = self.value
        if mask_secrets and self.is_secret:
            value = "********"
        return {
            "id": self.id,
            "project_id": self.project_id,
            "key": self.key,
            "value": value,
            "is_secret": self.is_secret,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
