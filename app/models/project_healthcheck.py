from app.extensions import db
from app.models.base import TimestampMixin


class ProjectHealthCheck(TimestampMixin, db.Model):
    __tablename__ = "project_healthchecks"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    deployment_id = db.Column(db.Integer, db.ForeignKey("deployments.id"), nullable=True, index=True)
    checked_at = db.Column(db.DateTime(timezone=True), nullable=False)
    target_url = db.Column(db.String(1000), nullable=False)
    success = db.Column(db.Boolean, nullable=False, default=False)
    status_code = db.Column(db.Integer, nullable=True)
    response_time_ms = db.Column(db.Integer, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    details = db.Column(db.JSON, nullable=True)

    project = db.relationship("Project", back_populates="health_checks")
    deployment = db.relationship("Deployment", foreign_keys=[deployment_id])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "deployment_id": self.deployment_id,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "target_url": self.target_url,
            "success": self.success,
            "status_code": self.status_code,
            "response_time_ms": self.response_time_ms,
            "error_message": self.error_message,
            "details": self.details,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
