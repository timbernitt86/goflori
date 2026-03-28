"""add rollback metadata to deployments

Revision ID: d4a7c9f1e2b3
Revises: c31d9a2e4f7b
Create Date: 2026-03-28 19:25:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d4a7c9f1e2b3"
down_revision = "c31d9a2e4f7b"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("successful", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("successful_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("source_snapshot_path", sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column("artifact_snapshot_path", sa.String(length=1000), nullable=True))

    op.execute("UPDATE deployments SET successful = 1 WHERE status = 'success'")


def downgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("artifact_snapshot_path")
        batch_op.drop_column("source_snapshot_path")
        batch_op.drop_column("successful_at")
        batch_op.drop_column("successful")
