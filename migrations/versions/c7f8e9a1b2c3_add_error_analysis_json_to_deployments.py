"""add error analysis json to deployments

Revision ID: c7f8e9a1b2c3
Revises: ab12c3d4e5f6
Create Date: 2026-03-29 22:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c7f8e9a1b2c3"
down_revision = "ab12c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("error_analysis_json", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("error_analysis_json")
