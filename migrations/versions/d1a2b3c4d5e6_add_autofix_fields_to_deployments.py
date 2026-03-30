"""add autofix fields to deployments

Revision ID: d1a2b3c4d5e6
Revises: c7f8e9a1b2c3
Create Date: 2026-03-29 23:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d1a2b3c4d5e6"
down_revision = "c7f8e9a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("autofix_status", sa.String(length=50), nullable=False, server_default="idle"))
        batch_op.add_column(sa.Column("autofix_attempt_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("last_autofix_action", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("last_autofix_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("autofix_history_json", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("autofix_history_json")
        batch_op.drop_column("last_autofix_at")
        batch_op.drop_column("last_autofix_action")
        batch_op.drop_column("autofix_attempt_count")
        batch_op.drop_column("autofix_status")
