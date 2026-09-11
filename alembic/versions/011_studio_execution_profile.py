"""Pin execution profiles for new scene runs without rewriting legacy plans."""

import sqlalchemy as sa

from alembic import op

revision = "011_studio_execution_profile"
down_revision = "010_studio_creative_planning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("studio_runs", sa.Column("execution_profile", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("studio_runs", "execution_profile")
