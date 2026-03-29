from app.extensions import db
from app.models.base import TimestampMixin


class DeploymentStep(TimestampMixin, db.Model):
    __tablename__ = "deployment_steps"

    id = db.Column(db.Integer, primary_key=True)
    deployment_id = db.Column(db.Integer, db.ForeignKey("deployments.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(50), nullable=False, default="pending")
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    stdout = db.Column(db.Text, nullable=True)
    stderr = db.Column(db.Text, nullable=True)
    exit_code = db.Column(db.Integer, nullable=True)
    json_details = db.Column(db.JSON, nullable=True)

    # Legacy fields kept for backward compatibility with existing UI/API code.
    output = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    order_index = db.Column(db.Integer, nullable=False, default=0)

    deployment = db.relationship("Deployment", back_populates="steps")

    def to_dict(self):
        resolved_stdout = self.stdout if self.stdout is not None else self.output
        resolved_stderr = self.stderr if self.stderr is not None else self.error_message
        details = self.json_details if isinstance(self.json_details, dict) else {}
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "stdout": resolved_stdout,
            "stderr": resolved_stderr,
            "exit_code": self.exit_code,
            "error_type": details.get("error_type"),
            "json_details": self.json_details,
            # Legacy payload keys for backward compatibility.
            "output": resolved_stdout,
            "error_message": resolved_stderr,
            "order_index": self.order_index,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
