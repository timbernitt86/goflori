from app.extensions import db
from app.models.base import TimestampMixin


class ProviderSetting(TimestampMixin, db.Model):
    __tablename__ = "provider_settings"

    id = db.Column(db.Integer, primary_key=True)
    provider_name = db.Column(db.String(100), nullable=False, unique=True)
    api_token = db.Column(db.Text, nullable=True)
    default_location = db.Column(db.String(100), nullable=True)
    default_server_type = db.Column(db.String(100), nullable=True)
    default_image = db.Column(db.String(255), nullable=True)
    ssh_key_name = db.Column(db.String(255), nullable=True)
    ssh_public_key = db.Column(db.Text, nullable=True)

    def to_dict(self, include_secrets: bool = False):
        return {
            "id": self.id,
            "provider_name": self.provider_name,
            "api_token": self.api_token if include_secrets else None,
            "api_token_configured": bool(self.api_token),
            "default_location": self.default_location,
            "default_server_type": self.default_server_type,
            "default_image": self.default_image,
            "ssh_key_name": self.ssh_key_name,
            "ssh_public_key": self.ssh_public_key if include_secrets else None,
            "ssh_public_key_configured": bool(self.ssh_public_key),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
