from app.extensions import db
from app.models.base import TimestampMixin


class DeploymentStep(TimestampMixin, db.Model):
    __tablename__ = "deployment_steps"

    id = db.Column(db.Integer, primary_key=True)
    deployment_id = db.Column(db.Integer, db.ForeignKey("deployments.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(50), nullable=False, default="pending")
    output = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    order_index = db.Column(db.Integer, nullable=False, default=0)

    deployment = db.relationship("Deployment", back_populates="steps")

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "name": self.name,
            "status": self.status,
            "output": self.output,
            "error_message": self.error_message,
            "order_index": self.order_index,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
