"""add ssh_private_key to provider_settings

Revision ID: e9a1b3c5d7f2
Revises: d4a7c9f1e2b3
Create Date: 2026-03-28 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "e9a1b3c5d7f2"
down_revision = "d4a7c9f1e2b3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("provider_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ssh_private_key", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("provider_settings", schema=None) as batch_op:
        batch_op.drop_column("ssh_private_key")
