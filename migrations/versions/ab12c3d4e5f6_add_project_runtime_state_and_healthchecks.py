"""add project runtime state and healthchecks

Revision ID: ab12c3d4e5f6
Revises: d4a7c9f1e2b3, 47dfa3c4365e
Create Date: 2026-03-29 20:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "ab12c3d4e5f6"
down_revision = ("d4a7c9f1e2b3", "47dfa3c4365e")
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("current_runtime_status", sa.String(length=50), nullable=False, server_default="failed"))
        batch_op.add_column(sa.Column("last_successful_deployment_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("active_deployment_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("active_version", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("active_source_reference", sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column("last_healthcheck_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key("fk_projects_last_successful_deployment", "deployments", ["last_successful_deployment_id"], ["id"])
        batch_op.create_foreign_key("fk_projects_active_deployment", "deployments", ["active_deployment_id"], ["id"])

    op.create_table(
        "project_healthchecks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_url", sa.String(length=1000), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_time_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_project_healthchecks_project_id"), "project_healthchecks", ["project_id"], unique=False)
    op.create_index(op.f("ix_project_healthchecks_deployment_id"), "project_healthchecks", ["deployment_id"], unique=False)

    op.execute(
        """
        UPDATE projects
        SET
            last_successful_deployment_id = (
                SELECT d.id
                FROM deployments d
                WHERE d.project_id = projects.id AND (d.successful = 1 OR d.status = 'success')
                ORDER BY COALESCE(d.successful_at, d.updated_at) DESC
                LIMIT 1
            ),
            active_deployment_id = (
                SELECT d.id
                FROM deployments d
                WHERE d.project_id = projects.id AND (d.successful = 1 OR d.status = 'success')
                ORDER BY COALESCE(d.successful_at, d.updated_at) DESC
                LIMIT 1
            ),
            active_version = (
                SELECT COALESCE(d.commit_sha, d.source_snapshot_path)
                FROM deployments d
                WHERE d.project_id = projects.id AND (d.successful = 1 OR d.status = 'success')
                ORDER BY COALESCE(d.successful_at, d.updated_at) DESC
                LIMIT 1
            ),
            active_source_reference = (
                SELECT d.source_snapshot_path
                FROM deployments d
                WHERE d.project_id = projects.id AND (d.successful = 1 OR d.status = 'success')
                ORDER BY COALESCE(d.successful_at, d.updated_at) DESC
                LIMIT 1
            ),
            current_runtime_status = CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM deployments d
                    WHERE d.project_id = projects.id AND (d.successful = 1 OR d.status = 'success')
                ) THEN 'degraded'
                ELSE 'failed'
            END
        """
    )


def downgrade():
    op.drop_index(op.f("ix_project_healthchecks_deployment_id"), table_name="project_healthchecks")
    op.drop_index(op.f("ix_project_healthchecks_project_id"), table_name="project_healthchecks")
    op.drop_table("project_healthchecks")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_constraint("fk_projects_active_deployment", type_="foreignkey")
        batch_op.drop_constraint("fk_projects_last_successful_deployment", type_="foreignkey")
        batch_op.drop_column("last_healthcheck_at")
        batch_op.drop_column("active_source_reference")
        batch_op.drop_column("active_version")
        batch_op.drop_column("active_deployment_id")
        batch_op.drop_column("last_successful_deployment_id")
        batch_op.drop_column("current_runtime_status")
