from app.extensions import db
from app.models.base import TimestampMixin


class ActivityLog(TimestampMixin, db.Model):
    __tablename__ = "activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    action = db.Column(db.String(255), nullable=False)
    actor = db.Column(db.String(255), nullable=False, default="system")
    message = db.Column(db.Text, nullable=False)

    project = db.relationship("Project", back_populates="activity_logs")

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "action": self.action,
            "actor": self.actor,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
