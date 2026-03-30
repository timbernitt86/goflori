"""add light monitoring to projects

Revision ID: e2f3a4b5c6d7
Revises: d1a2b3c4d5e6
Create Date: 2026-03-30 10:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "e2f3a4b5c6d7"
down_revision = "d1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("light_monitoring_status", sa.String(length=50), nullable=False, server_default="unknown"))
        batch_op.add_column(sa.Column("light_monitoring_json", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("light_monitoring_json")
        batch_op.drop_column("light_monitoring_status")
