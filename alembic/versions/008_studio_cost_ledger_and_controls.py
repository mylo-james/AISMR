"""Add versioned Studio cost accounting and durable admission controls.

Revision ID: 008_studio_cost_ledger_and_controls
Revises: 007_studio_gallery_projection_intents
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "008_studio_cost_ledger_and_controls"
down_revision: str | None = "007_studio_gallery_projection_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = sa.String(36).with_variant(postgresql.UUID(as_uuid=True), "postgresql")
    with op.batch_alter_table("studio_runs") as batch:
        batch.add_column(sa.Column("plan_review_created_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("plan_review_expires_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("final_review_created_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("final_review_expires_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("studio_reservations") as batch:
        batch.add_column(sa.Column("cost_profile_version", sa.String(96), nullable=True))
    op.create_table(
        "studio_cost_ledger",
        sa.Column("id", uuid_type, primary_key=True, nullable=False),
        sa.Column("run_id", uuid_type, sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("profile_version", sa.String(96), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("operation_key", sa.String(160), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("estimated_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("confirmed_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("cost_state", sa.String(24), nullable=False),
        sa.Column("receipt_reference", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reconciled_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_studio_cost_ledger_run_id", "studio_cost_ledger", ["run_id"])
    op.create_index(
        "ix_studio_cost_ledger_state", "studio_cost_ledger", ["cost_state", "created_at"]
    )
    op.create_index(
        "ux_studio_cost_ledger_operation",
        "studio_cost_ledger",
        ["run_id", "profile_version", "stage", "operation_key"],
        unique=True,
    )
    op.create_table(
        "studio_admission_control",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(160), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("studio_admission_control")
    op.drop_table("studio_cost_ledger")
    with op.batch_alter_table("studio_reservations") as batch:
        batch.drop_column("cost_profile_version")
    with op.batch_alter_table("studio_runs") as batch:
        batch.drop_column("final_review_expires_at")
        batch.drop_column("final_review_created_at")
        batch.drop_column("plan_review_expires_at")
        batch.drop_column("plan_review_created_at")
