"""Fence durable job claims with a monotonically increasing generation.

Revision ID: 006_job_claim_generation
Revises: 005_monthly_studio
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006_job_claim_generation"
down_revision: Union[str, None] = "005_monthly_studio"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("claim_generation", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("jobs", "claim_generation")
