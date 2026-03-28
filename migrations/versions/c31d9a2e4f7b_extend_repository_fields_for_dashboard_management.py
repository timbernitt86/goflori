"""extend repository fields for dashboard management

Revision ID: c31d9a2e4f7b
Revises: b7f1d2e9c4a1
Create Date: 2026-03-28 16:35:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c31d9a2e4f7b"
down_revision = "b7f1d2e9c4a1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("repositories", schema=None) as batch_op:
        batch_op.add_column(sa.Column("repo_url", sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column("access_token", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.execute("UPDATE repositories SET repo_url = url WHERE repo_url IS NULL")

    with op.batch_alter_table("repositories", schema=None) as batch_op:
        batch_op.alter_column("repo_url", existing_type=sa.String(length=1000), nullable=False)
        batch_op.alter_column("provider", existing_type=sa.String(length=50), nullable=True)


def downgrade():
    with op.batch_alter_table("repositories", schema=None) as batch_op:
        batch_op.alter_column("provider", existing_type=sa.String(length=50), nullable=False)
        batch_op.drop_column("is_private")
        batch_op.drop_column("access_token")
        batch_op.drop_column("repo_url")
