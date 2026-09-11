"""Add versioned creative planning receipts without changing existing runs."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "010_studio_creative_planning"
down_revision = "009_studio_cost_confirmation_timestamps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = sa.String(36).with_variant(postgresql.UUID(as_uuid=True), "postgresql")
    op.create_table(
        "studio_planner_runs",
        sa.Column("run_id", uuid_type, sa.ForeignKey("studio_runs.run_id"), primary_key=True),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_studio_planner_runs_expires_at", "studio_planner_runs", ["expires_at"])
    op.create_table(
        "studio_planning_operations",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("run_id", uuid_type, sa.ForeignKey("studio_runs.run_id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ux_studio_planning_role",
        "studio_planning_operations",
        ["run_id", "revision", "role"],
        unique=True,
    )
    op.create_index(
        "ix_studio_planning_operations_run_id", "studio_planning_operations", ["run_id"]
    )
    op.create_index(
        "ix_studio_planning_operations_expires_at", "studio_planning_operations", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("studio_planning_operations")
    op.drop_table("studio_planner_runs")
