from app.extensions import db
from app.models.base import TimestampMixin


class Deployment(TimestampMixin, db.Model):
    __tablename__ = "deployments"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    server_id = db.Column(db.Integer, db.ForeignKey("servers.id"), nullable=True)
    status = db.Column(db.String(50), nullable=False, default="pending")
    mode = db.Column(db.String(50), nullable=False, default="staging")
    trigger_source = db.Column(db.String(50), nullable=False, default="manual")
    commit_sha = db.Column(db.String(100), nullable=True)
    successful = db.Column(db.Boolean, nullable=False, default=False)
    successful_at = db.Column(db.DateTime(timezone=True), nullable=True)
    source_snapshot_path = db.Column(db.String(1000), nullable=True)
    artifact_snapshot_path = db.Column(db.String(1000), nullable=True)
    output = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    project = db.relationship("Project", back_populates="deployments", foreign_keys=[project_id])
    server = db.relationship("Server", foreign_keys=[server_id])
    steps = db.relationship("DeploymentStep", back_populates="deployment", cascade="all, delete-orphan")

    def to_dict(self, include_steps: bool = False):
        data = {
            "id": self.id,
            "project_id": self.project_id,
            "server_id": self.server_id,
            "status": self.status,
            "mode": self.mode,
            "trigger_source": self.trigger_source,
            "commit_sha": self.commit_sha,
            "successful": self.successful,
            "successful_at": self.successful_at.isoformat() if self.successful_at else None,
            "source_snapshot_path": self.source_snapshot_path,
            "artifact_snapshot_path": self.artifact_snapshot_path,
            "output": self.output,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if include_steps:
            data["steps"] = [step.to_dict() for step in self.steps]
        return data
