"""add server assignment to projects and deployments

Revision ID: 8d2c4a9e7b11
Revises: 3a5d8c1f4b2e
Create Date: 2026-03-27 13:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8d2c4a9e7b11"
down_revision = "3a5d8c1f4b2e"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("active_server_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_projects_active_server_id_servers",
            "servers",
            ["active_server_id"],
            ["id"],
        )

    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("server_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_deployments_server_id_servers",
            "servers",
            ["server_id"],
            ["id"],
        )


def downgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_constraint("fk_deployments_server_id_servers", type_="foreignkey")
        batch_op.drop_column("server_id")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_constraint("fk_projects_active_server_id_servers", type_="foreignkey")
        batch_op.drop_column("active_server_id")
