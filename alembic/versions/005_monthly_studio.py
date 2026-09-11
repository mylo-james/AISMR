"""Add the visitor-operated AISMR monthly studio tables."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "005_monthly_studio"
down_revision = "004_public_demo_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = sa.String(36).with_variant(postgresql.UUID(as_uuid=True), "postgresql")
    op.create_table("studio_visitors",
        sa.Column('id', sa.String(32), primary_key=True, nullable=False),
        sa.Column('secret_hash', sa.String(64), nullable=False),
        sa.Column('ip_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_studio_visitors_ip_hash', 'studio_visitors', ['ip_hash'], unique=False)
    op.create_table("studio_admission_lock",
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
    )
    op.create_table("studio_runs",
        sa.Column('run_id', uuid_type, sa.ForeignKey('runs.id'), primary_key=True, nullable=False),
        sa.Column('visitor_id', sa.String(32), sa.ForeignKey('studio_visitors.id'), nullable=False),
        sa.Column('start_key', sa.String(64), nullable=False),
        sa.Column('read_token_hash', sa.String(64), unique=True, nullable=False),
        sa.Column('item_id', sa.String(48), nullable=False),
        sa.Column('item_text', sa.String(48), nullable=False),
        sa.Column('mode', sa.String(16), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('event_sequence', sa.Integer(), nullable=False),
        sa.Column('plan', sa.JSON(), nullable=True),
        sa.Column('plan_hash', sa.String(64), nullable=True),
        sa.Column('plan_approved_hash', sa.String(64), nullable=True),
        sa.Column('status', sa.String(40), nullable=False),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('moderation', sa.JSON(), nullable=True),
        sa.Column('cancelled', sa.Boolean(), nullable=False),
        sa.Column('render_job_id', sa.String(128), nullable=True),
        sa.Column('render_input_hash', sa.String(64), nullable=True),
        sa.Column('render_submission_state', sa.String(32), nullable=True),
        sa.Column('final_artifact_id', uuid_type, sa.ForeignKey('artifacts.id'), nullable=True),
        sa.Column('final_hash', sa.String(64), nullable=True),
        sa.Column('final_metadata', sa.JSON(), nullable=True),
        sa.Column('publish_config', sa.JSON(), nullable=True),
        sa.Column('approved_publish_hash', sa.String(64), nullable=True),
        sa.Column('publish_request_id', sa.String(160), nullable=True),
        sa.Column('publish_state', sa.String(32), nullable=True),
        sa.Column('tiktok_post_id', sa.String(40), nullable=True),
        sa.Column('tiktok_url', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_studio_runs_visitor_id', 'studio_runs', ['visitor_id'], unique=False)
    op.create_index('ux_studio_start', 'studio_runs', ['visitor_id', 'start_key'], unique=True)
    op.create_table("studio_reservations",
        sa.Column('run_id', uuid_type, sa.ForeignKey('runs.id'), primary_key=True, nullable=False),
        sa.Column('visitor_id', sa.String(32), sa.ForeignKey('studio_visitors.id'), nullable=True),
        sa.Column('ip_hash', sa.String(64), nullable=True),
        sa.Column('reserved_usd', sa.Numeric(12, 6), nullable=False),
        sa.Column('confirmed_usd', sa.Numeric(12, 6), nullable=True),
        sa.Column('estimated_usd', sa.Numeric(12, 6), nullable=True),
        sa.Column('cost_state', sa.String(24), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_studio_reservations_created_at', 'studio_reservations', ['created_at'], unique=False)
    op.create_index('ix_studio_reservations_ip_hash', 'studio_reservations', ['ip_hash'], unique=False)
    op.create_index('ix_studio_reservations_visitor_id', 'studio_reservations', ['visitor_id'], unique=False)
    op.create_table("studio_decisions",
        sa.Column('id', uuid_type, primary_key=True, nullable=False),
        sa.Column('run_id', uuid_type, sa.ForeignKey('runs.id'), nullable=False),
        sa.Column('visitor_id', sa.String(32), sa.ForeignKey('studio_visitors.id'), nullable=False),
        sa.Column('gate', sa.String(24), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('subject_hash', sa.String(64), nullable=False),
        sa.Column('request_key', sa.String(64), nullable=False),
        sa.Column('decision', sa.String(24), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_studio_decisions_run_id', 'studio_decisions', ['run_id'], unique=False)
    op.create_index('ux_studio_decision', 'studio_decisions', ['run_id', 'request_key'], unique=True)
    op.create_table("studio_assets",
        sa.Column('id', uuid_type, primary_key=True, nullable=False),
        sa.Column('run_id', uuid_type, sa.ForeignKey('runs.id'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('input_hash', sa.String(64), nullable=False),
        sa.Column('operation_key', sa.String(160), unique=True, nullable=False),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('model', sa.String(160), nullable=True),
        sa.Column('request_id', sa.String(160), nullable=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('queue_position', sa.Integer(), nullable=True),
        sa.Column('artifact_id', uuid_type, sa.ForeignKey('artifacts.id'), nullable=True),
        sa.Column('sha256', sa.String(64), nullable=True),
        sa.Column('media_metadata', sa.JSON(), nullable=True),
        sa.Column('safety', sa.JSON(), nullable=True),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('submitted_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_studio_assets_run_id', 'studio_assets', ['run_id'], unique=False)
    op.create_index('ux_studio_asset_attempt', 'studio_assets', ['run_id', 'revision', 'ordinal', 'kind', 'attempt'], unique=True)
    op.create_table("studio_events",
        sa.Column('id', uuid_type, primary_key=True, nullable=False),
        sa.Column('run_id', uuid_type, sa.ForeignKey('runs.id'), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('stage', sa.String(40), nullable=False),
        sa.Column('event_type', sa.String(48), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_studio_events_run_id', 'studio_events', ['run_id'], unique=False)
    op.create_index('ux_studio_event_sequence', 'studio_events', ['run_id', 'sequence'], unique=True)


def downgrade() -> None:
    op.drop_table('studio_events')
    op.drop_table('studio_assets')
    op.drop_table('studio_decisions')
    op.drop_table('studio_reservations')
    op.drop_table('studio_runs')
    op.drop_table('studio_admission_lock')
    op.drop_table('studio_visitors')
