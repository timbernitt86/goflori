from app.extensions import db
from app.models.base import TimestampMixin


class Server(TimestampMixin, db.Model):
    __tablename__ = "servers"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    provider = db.Column(db.String(50), nullable=False, default="hetzner")
    provider_server_id = db.Column(db.String(255), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    server_type = db.Column(db.String(100), nullable=False, default="cx22")
    region = db.Column(db.String(100), nullable=False, default="nbg1")
    ipv4 = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(50), nullable=False, default="provisioning")

    project = db.relationship("Project", back_populates="servers", foreign_keys=[project_id])

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "provider": self.provider,
            "provider_server_id": self.provider_server_id,
            "name": self.name,
            "server_type": self.server_type,
            "region": self.region,
            "ipv4": self.ipv4,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
