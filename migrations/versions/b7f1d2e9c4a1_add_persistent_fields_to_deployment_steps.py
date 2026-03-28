"""add persistent fields to deployment steps

Revision ID: b7f1d2e9c4a1
Revises: 8d2c4a9e7b11
Create Date: 2026-03-28 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b7f1d2e9c4a1"
down_revision = "8d2c4a9e7b11"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("deployment_steps", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deployment_steps", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deployment_steps", sa.Column("stdout", sa.Text(), nullable=True))
    op.add_column("deployment_steps", sa.Column("stderr", sa.Text(), nullable=True))
    op.add_column("deployment_steps", sa.Column("exit_code", sa.Integer(), nullable=True))
    op.add_column("deployment_steps", sa.Column("json_details", sa.JSON(), nullable=True))

    op.execute("UPDATE deployment_steps SET started_at = created_at WHERE started_at IS NULL")
    op.execute("UPDATE deployment_steps SET finished_at = updated_at WHERE status IN ('success', 'failed') AND finished_at IS NULL")
    op.execute("UPDATE deployment_steps SET stdout = output WHERE stdout IS NULL AND output IS NOT NULL")
    op.execute("UPDATE deployment_steps SET stderr = error_message WHERE stderr IS NULL AND error_message IS NOT NULL")
    op.execute("UPDATE deployment_steps SET exit_code = 0 WHERE status = 'success' AND exit_code IS NULL")
    op.execute("UPDATE deployment_steps SET exit_code = 1 WHERE status = 'failed' AND exit_code IS NULL")


def downgrade():
    op.drop_column("deployment_steps", "json_details")
    op.drop_column("deployment_steps", "exit_code")
    op.drop_column("deployment_steps", "stderr")
    op.drop_column("deployment_steps", "stdout")
    op.drop_column("deployment_steps", "finished_at")
    op.drop_column("deployment_steps", "started_at")
