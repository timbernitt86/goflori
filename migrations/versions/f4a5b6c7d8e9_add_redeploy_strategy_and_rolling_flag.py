"""add redeploy strategy and rolling flag

Revision ID: f4a5b6c7d8e9
Revises: e2f3a4b5c6d7
Create Date: 2026-03-30 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "f4a5b6c7d8e9"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("rolling_update_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))

    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("redeploy_strategy", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("minimal_downtime_attempted", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("rolling_update_enabled_snapshot", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("rolling_update_enabled_snapshot")
        batch_op.drop_column("minimal_downtime_attempted")
        batch_op.drop_column("redeploy_strategy")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("rolling_update_enabled")
