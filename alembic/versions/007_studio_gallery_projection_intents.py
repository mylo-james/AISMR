"""Add source-owned outbox rows for cross-mode portfolio projections.

Revision ID: 007_studio_gallery_projection_intents
Revises: 006_job_claim_generation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "007_studio_gallery_projection_intents"
down_revision: str | None = "006_job_claim_generation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = sa.String(36).with_variant(postgresql.UUID(as_uuid=True), "postgresql")
    op.create_table(
        "studio_gallery_projection_intents",
        sa.Column("id", uuid_type, primary_key=True, nullable=False),
        sa.Column("run_id", uuid_type, sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("final_hash", sa.String(64), nullable=False),
        sa.Column("decision_key", sa.String(64), nullable=False),
        sa.Column("consent_subject_hash", sa.String(64), nullable=False),
        sa.Column("source_instance_key", sa.String(160), nullable=False),
        sa.Column("source_mode", sa.String(16), nullable=False),
        sa.Column("item_label", sa.String(96), nullable=False),
        sa.Column("public_suitability_receipt", sa.String(160), nullable=False),
        sa.Column("rights_receipt", sa.String(160), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("library_entry_id", uuid_type, nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_studio_gallery_projection_intents_run_id",
        "studio_gallery_projection_intents",
        ["run_id"],
    )
    op.create_index(
        "ix_studio_gallery_projection_intents_state",
        "studio_gallery_projection_intents",
        ["state", "created_at"],
    )
    op.create_index(
        "ux_studio_gallery_projection_source",
        "studio_gallery_projection_intents",
        ["source_instance_key"],
        unique=True,
    )
    op.create_index(
        "ux_studio_gallery_projection_decision",
        "studio_gallery_projection_intents",
        ["run_id", "revision", "final_hash", "decision_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("studio_gallery_projection_intents")
