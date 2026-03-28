import re

from app.extensions import db
from app.models.base import TimestampMixin


class Company(TimestampMixin, db.Model):
    __tablename__ = "companies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), unique=True, nullable=False)

    users = db.relationship("User", back_populates="company", cascade="all, delete-orphan")
    projects = db.relationship("Project", back_populates="company", cascade="all, delete-orphan")

    @staticmethod
    def slugify(value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r"[^a-z0-9]+", "-", value)
        return value.strip("-")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
