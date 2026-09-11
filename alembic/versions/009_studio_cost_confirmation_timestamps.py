"""Persist provider-confirmed cost timestamps for period accounting.

Revision ID: 009_studio_cost_confirmation_timestamps
Revises: 008_studio_cost_ledger_and_controls
"""

import sqlalchemy as sa

from alembic import op

revision = "009_studio_cost_confirmation_timestamps"
down_revision = "008_studio_cost_ledger_and_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("studio_cost_ledger") as batch:
        batch.add_column(sa.Column("confirmed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("studio_cost_ledger") as batch:
        batch.drop_column("confirmed_at")
