"""add project infrastructure preferences

Revision ID: 3a5d8c1f4b2e
Revises: 93bba3efba06
Create Date: 2026-03-27 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "3a5d8c1f4b2e"
down_revision = "93bba3efba06"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("desired_server_type", sa.String(length=100), nullable=True))
    op.add_column("projects", sa.Column("desired_location", sa.String(length=100), nullable=True))
    op.add_column("projects", sa.Column("desired_image", sa.String(length=255), nullable=True))


def downgrade():
    op.drop_column("projects", "desired_image")
    op.drop_column("projects", "desired_location")
    op.drop_column("projects", "desired_server_type")
