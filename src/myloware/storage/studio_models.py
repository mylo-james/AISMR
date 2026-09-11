"""Persistent state for the AISMR visitor journey, using the existing SQL database."""

from __future__ import annotations

import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base

from myloware.storage.models import GUID, Base, _utc_now

# The public portfolio library deliberately has separate metadata from the
# per-mode Studio database.  It is not registered on ``Base``: callers create
# these tables only on the separately configured library connection.
PortfolioLibraryBase = declarative_base()


class StudioVisitor(Base):
    __tablename__ = "studio_visitors"
    id = Column(String(32), primary_key=True)
    secret_hash = Column(String(64), nullable=False)
    ip_hash = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, default=_utc_now, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class StudioAdmissionLock(Base):
    """One database row serializes admission on SQLite and PostgreSQL."""

    __tablename__ = "studio_admission_lock"
    id = Column(Integer, primary_key=True)
    generation = Column(Integer, nullable=False, default=0)


class StudioRun(Base):
    __tablename__ = "studio_runs"
    __table_args__ = (Index("ux_studio_start", "visitor_id", "start_key", unique=True),)
    run_id = Column(GUID(), ForeignKey("runs.id"), primary_key=True)
    visitor_id = Column(String(32), ForeignKey("studio_visitors.id"), nullable=False, index=True)
    start_key = Column(String(64), nullable=False)
    read_token_hash = Column(String(64), nullable=False, unique=True)
    item_id = Column(String(48), nullable=False)
    item_text = Column(String(48), nullable=False)
    mode = Column(String(16), nullable=False)
    execution_profile = Column(JSON, nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    event_sequence = Column(Integer, nullable=False, default=0)
    plan = Column(JSON, nullable=True)
    plan_hash = Column(String(64), nullable=True)
    plan_approved_hash = Column(String(64), nullable=True)
    plan_review_created_at = Column(DateTime, nullable=True)
    plan_review_expires_at = Column(DateTime, nullable=True)
    status = Column(String(40), nullable=False, default="ideating")
    error_code = Column(String(64), nullable=True)
    moderation = Column(JSON, nullable=True)
    cancelled = Column(Boolean, nullable=False, default=False)
    render_job_id = Column(String(128), nullable=True)
    render_input_hash = Column(String(64), nullable=True)
    render_submission_state = Column(String(32), nullable=True)
    final_artifact_id = Column(GUID(), ForeignKey("artifacts.id"), nullable=True)
    final_hash = Column(String(64), nullable=True)
    final_metadata = Column(JSON, nullable=True)
    final_review_created_at = Column(DateTime, nullable=True)
    final_review_expires_at = Column(DateTime, nullable=True)
    publish_config = Column(JSON, nullable=True)
    approved_publish_hash = Column(String(64), nullable=True)
    publish_request_id = Column(String(160), nullable=True)
    publish_state = Column(String(32), nullable=True)
    tiktok_post_id = Column(String(40), nullable=True)
    tiktok_url = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utc_now, nullable=False)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class StudioPlannerRun(Base):
    """Admission-pinned planning behavior; absent rows retain the v1 workflow."""

    __tablename__ = "studio_planner_runs"
    run_id = Column(GUID(), ForeignKey("studio_runs.run_id"), primary_key=True)
    version = Column(String(32), nullable=False)
    configuration = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=_utc_now, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


class StudioPlanningOperation(Base):
    """Bounded inputs and receipts, never a conversation or hidden reasoning log."""

    __tablename__ = "studio_planning_operations"
    __table_args__ = (Index("ux_studio_planning_role", "run_id", "revision", "role", unique=True),)
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id = Column(GUID(), ForeignKey("studio_runs.run_id"), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    role = Column(String(32), nullable=False)
    input_hash = Column(String(64), nullable=False)
    request = Column(JSON, nullable=False)
    response = Column(JSON, nullable=True)
    status = Column(String(24), nullable=False)
    error_code = Column(String(64), nullable=True)
    deadline_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=_utc_now, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)


class StudioReservation(Base):
    __tablename__ = "studio_reservations"
    run_id = Column(GUID(), ForeignKey("runs.id"), primary_key=True)
    visitor_id = Column(String(32), ForeignKey("studio_visitors.id"), nullable=True, index=True)
    ip_hash = Column(String(64), nullable=True, index=True)
    reserved_usd = Column(Numeric(12, 6), nullable=False)
    confirmed_usd = Column(Numeric(12, 6), nullable=True)
    estimated_usd = Column(Numeric(12, 6), nullable=True)
    cost_state = Column(String(24), nullable=False, default="reserved")
    cost_profile_version = Column(String(96), nullable=True)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utc_now, nullable=False, index=True)


class StudioCostLedger(Base):
    """Stage-level reservation and receipt state for one versioned run profile."""

    __tablename__ = "studio_cost_ledger"
    __table_args__ = (
        Index(
            "ux_studio_cost_ledger_operation",
            "run_id",
            "profile_version",
            "stage",
            "operation_key",
            unique=True,
        ),
        Index("ix_studio_cost_ledger_state", "cost_state", "created_at"),
    )
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id = Column(GUID(), ForeignKey("runs.id"), nullable=False, index=True)
    profile_version = Column(String(96), nullable=False)
    stage = Column(String(64), nullable=False)
    operation_key = Column(String(160), nullable=False)
    reserved_usd = Column(Numeric(12, 6), nullable=False)
    estimated_usd = Column(Numeric(12, 6), nullable=True)
    confirmed_usd = Column(Numeric(12, 6), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    cost_state = Column(String(24), nullable=False, default="reserved")
    receipt_reference = Column(String(160), nullable=True)
    created_at = Column(DateTime, default=_utc_now, nullable=False, index=True)
    reconciled_at = Column(DateTime, nullable=True)


class StudioAdmissionControl(Base):
    """Durable owner-controlled pause for new Studio admissions."""

    __tablename__ = "studio_admission_control"
    id = Column(Integer, primary_key=True)
    paused = Column(Boolean, nullable=False, default=False)
    reason = Column(String(160), nullable=True)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now, nullable=False)


class StudioDecision(Base):
    __tablename__ = "studio_decisions"
    __table_args__ = (Index("ux_studio_decision", "run_id", "request_key", unique=True),)
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id = Column(GUID(), ForeignKey("runs.id"), nullable=False, index=True)
    visitor_id = Column(String(32), ForeignKey("studio_visitors.id"), nullable=False)
    gate = Column(String(24), nullable=False)
    revision = Column(Integer, nullable=False)
    subject_hash = Column(String(64), nullable=False)
    request_key = Column(String(64), nullable=False)
    decision = Column(String(24), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=_utc_now, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class StudioAsset(Base):
    __tablename__ = "studio_assets"
    __table_args__ = (
        Index(
            "ux_studio_asset_attempt",
            "run_id",
            "revision",
            "ordinal",
            "kind",
            "attempt",
            unique=True,
        ),
    )
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id = Column(GUID(), ForeignKey("runs.id"), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    ordinal = Column(Integer, nullable=False)
    kind = Column(String(16), nullable=False)
    attempt = Column(Integer, nullable=False, default=1)
    input_hash = Column(String(64), nullable=False)
    operation_key = Column(String(160), nullable=False, unique=True)
    provider = Column(String(32), nullable=False)
    model = Column(String(160), nullable=True)
    request_id = Column(String(160), nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    queue_position = Column(Integer, nullable=True)
    artifact_id = Column(GUID(), ForeignKey("artifacts.id"), nullable=True)
    sha256 = Column(String(64), nullable=True)
    media_metadata = Column(JSON, nullable=True)
    safety = Column(JSON, nullable=True)
    error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=_utc_now, nullable=False)
    submitted_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class StudioEvent(Base):
    __tablename__ = "studio_events"
    __table_args__ = (Index("ux_studio_event_sequence", "run_id", "sequence", unique=True),)
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id = Column(GUID(), ForeignKey("runs.id"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    stage = Column(String(40), nullable=False)
    event_type = Column(String(48), nullable=False)
    detail = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=_utc_now, nullable=False)


class StudioGalleryProjectionIntent(Base):
    """Source-transactional intent to project one explicitly public final.

    This belongs to the run's mode-specific database.  It deliberately stores
    no visitor token, provider request, or source filesystem path: the worker
    derives ``<mode media root>/<run id>/final.mp4`` from its trusted config.
    """

    __tablename__ = "studio_gallery_projection_intents"
    __table_args__ = (
        Index(
            "ux_studio_gallery_projection_decision",
            "run_id",
            "revision",
            "final_hash",
            "decision_key",
            unique=True,
        ),
        Index("ux_studio_gallery_projection_source", "source_instance_key", unique=True),
        Index("ix_studio_gallery_projection_state", "state", "created_at"),
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id = Column(GUID(), ForeignKey("runs.id"), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    final_hash = Column(String(64), nullable=False)
    decision_key = Column(String(64), nullable=False)
    consent_subject_hash = Column(String(64), nullable=False)
    source_instance_key = Column(String(160), nullable=False)
    source_mode = Column(String(16), nullable=False)
    item_label = Column(String(96), nullable=False)
    public_suitability_receipt = Column(String(160), nullable=False)
    rights_receipt = Column(String(160), nullable=False)
    history = Column(JSON, nullable=False, default=dict)
    state = Column(String(24), nullable=False, default="pending", index=True)
    claim_generation = Column(Integer, nullable=False, default=0)
    library_entry_id = Column(GUID(), nullable=True)
    created_at = Column(DateTime, default=_utc_now, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class StudioGalleryLock(PortfolioLibraryBase):
    """One row used to serialize activation on SQLite and PostgreSQL."""

    __tablename__ = "studio_gallery_lock"
    id = Column(Integer, primary_key=True)
    generation = Column(Integer, nullable=False, default=0)


class StudioGalleryEntry(PortfolioLibraryBase):
    """Sanitized, cross-mode projection of an explicitly public final."""

    __tablename__ = "studio_gallery_entries"
    __table_args__ = (
        Index("ux_studio_gallery_source", "source_instance_key", unique=True),
        Index("ix_studio_gallery_active_rank", "state", "accepted_at", "source_instance_key"),
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    source_instance_key = Column(String(160), nullable=False)
    source_run_id = Column(GUID(), nullable=False, index=True)
    source_revision = Column(Integer, nullable=False)
    source_mode = Column(String(16), nullable=False)
    item_label = Column(String(96), nullable=False)
    final_sha256 = Column(String(64), nullable=False)
    accepted_at = Column(DateTime, nullable=False, index=True)
    public_suitability_receipt = Column(String(160), nullable=False)
    rights_receipt = Column(String(160), nullable=False)
    consent_subject_hash = Column(String(64), nullable=False)
    serving_key = Column(String(96), nullable=False, unique=True)
    history = Column(JSON, nullable=False, default=dict)
    state = Column(String(24), nullable=False, default="copy_pending", index=True)
    cleanup_intent = Column(String(24), nullable=True)
    created_at = Column(DateTime, default=_utc_now, nullable=False)
    activated_at = Column(DateTime, nullable=True)
    retired_at = Column(DateTime, nullable=True)


class StudioGalleryOutbox(PortfolioLibraryBase):
    """Durable cleanup/reconciliation work for an owned serving copy."""

    __tablename__ = "studio_gallery_outbox"
    __table_args__ = (Index("ux_studio_gallery_outbox", "entry_id", "kind", unique=True),)

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    entry_id = Column(GUID(), ForeignKey("studio_gallery_entries.id"), nullable=False, index=True)
    kind = Column(String(24), nullable=False)
    state = Column(String(24), nullable=False, default="pending", index=True)
    claim_generation = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_utc_now, nullable=False)
    completed_at = Column(DateTime, nullable=True)
