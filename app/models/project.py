import re

from app.extensions import db
from app.models.base import TimestampMixin


class Project(TimestampMixin, db.Model):
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), unique=True, nullable=False)
    framework = db.Column(db.String(50), nullable=True)
    environment = db.Column(db.String(50), nullable=False, default="production")
    domain = db.Column(db.String(255), nullable=True)
    active_server_id = db.Column(db.Integer, db.ForeignKey("servers.id"), nullable=True)
    desired_server_type = db.Column(db.String(100), nullable=True)
    desired_location = db.Column(db.String(100), nullable=True)
    desired_image = db.Column(db.String(255), nullable=True)
    branch = db.Column(db.String(255), nullable=False, default="main")
    status = db.Column(db.String(50), nullable=False, default="draft")

    repository = db.relationship("Repository", back_populates="project", uselist=False, cascade="all, delete-orphan")
    servers = db.relationship(
        "Server",
        back_populates="project",
        cascade="all, delete-orphan",
        foreign_keys="Server.project_id",
    )
    active_server = db.relationship("Server", foreign_keys=[active_server_id], post_update=True)
    deployments = db.relationship("Deployment", back_populates="project", cascade="all, delete-orphan")
    environment_variables = db.relationship(
        "EnvironmentVariable", back_populates="project", cascade="all, delete-orphan"
    )
    activity_logs = db.relationship("ActivityLog", back_populates="project", cascade="all, delete-orphan")
    company = db.relationship("Company", back_populates="projects")

    @staticmethod
    def slugify(value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r"[^a-z0-9]+", "-", value)
        return value.strip("-")

    def to_dict(self, include_children: bool = False):
        data = {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "framework": self.framework,
            "environment": self.environment,
            "domain": self.domain,
            "active_server_id": self.active_server_id,
            "desired_server_type": self.desired_server_type,
            "desired_location": self.desired_location,
            "desired_image": self.desired_image,
            "branch": self.branch,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if include_children:
            data["repository"] = self.repository.to_dict() if self.repository else None
            data["servers"] = [server.to_dict() for server in self.servers]
            data["deployments"] = [deployment.to_dict() for deployment in self.deployments]
            data["env"] = [item.to_dict(mask_secrets=True) for item in self.environment_variables]
        return data
