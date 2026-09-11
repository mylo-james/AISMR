"""SQLAlchemy database models for AISMR."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import uuid
from typing import Any, Dict
from uuid import UUID

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    JSON,
    ForeignKey,
    Index,
    TypeDecorator,
    BigInteger,
    Boolean,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import declarative_base, relationship


def _utc_now() -> datetime:
    """Return a tz-naive UTC timestamp for TIMESTAMP WITHOUT TIME ZONE columns.

    Using tz-aware values with asyncpg against TIMESTAMP WITHOUT TIME ZONE columns
    triggers "can't subtract offset-naive and offset-aware datetimes", so we keep
    these fields naive and treat them as UTC by convention.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class GUID(TypeDecorator[UUID]):
    """Platform-independent GUID type.

    Uses PostgreSQL's UUID type when available, otherwise stores as String(36).
    This enables unit tests with SQLite while using native UUIDs in production PostgreSQL.
    """

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        else:
            return dialect.type_descriptor(String(36))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return value
        elif dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
        else:
            return str(value) if isinstance(value, uuid.UUID) else value

    def process_result_value(self, value: Any, dialect: Any) -> UUID | None:
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


Base = declarative_base()

__all__ = [
    "Base",
    "Run",
    "RunStatus",
    "Artifact",
    "ArtifactType",
    "GUID",
    "Job",
    "JobStatus",
    "ChatSession",
    "AuditLog",
    "Feedback",
    "DeadLetter",
]


class RunStatus(str, Enum):
    """Status of a workflow run."""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_IDEATION_APPROVAL = "awaiting_ideation_approval"
    AWAITING_VIDEO_GENERATION = "awaiting_video_generation"  # Waiting for OpenAI Sora webhooks
    AWAITING_RENDER = "awaiting_render"  # Waiting for Remotion webhook
    AWAITING_PUBLISH = "awaiting_publish"  # Waiting for upload-post status/publish webhook
    AWAITING_PUBLISH_APPROVAL = "awaiting_publish_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class Run(Base):
    """Workflow run model."""

    __tablename__ = "runs"

    id = Column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
    )
    workflow_name = Column(String(64), nullable=False)
    vector_db_id = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default=RunStatus.PENDING.value)
    current_step = Column(String(64), nullable=True)
    user_id = Column(String(128), nullable=True, index=True)
    telegram_chat_id = Column(BigInteger, nullable=True, index=True)
    input = Column(Text, nullable=False)
    artifacts = Column(JSON, default=dict)
    error = Column(Text, nullable=True)
    llama_stack_trace_id = Column(String(64), nullable=True)
    public_demo = Column(Boolean, default=False, nullable=False)
    public_token = Column(String(64), nullable=True, unique=True, index=True)
    public_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utc_now)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now)
    artifacts_rel = relationship("Artifact", back_populates="run")
    jobs_rel = relationship("Job", back_populates="run")

    # Backwards compatibility: allow setting artifacts_dict too
    @property
    def artifacts_dict(self) -> Dict[str, Any]:
        return self.artifacts or {}

    @artifacts_dict.setter
    def artifacts_dict(self, value: Dict[str, Any]) -> None:
        self.artifacts = value

    def to_dict(self) -> Dict[str, Any]:
        """Convert run to dictionary."""
        return {
            "id": str(self.id),
            "workflow_name": self.workflow_name,
            "vector_db_id": self.vector_db_id,
            "status": self.status,
            "current_step": self.current_step,
            "user_id": self.user_id,
            "telegram_chat_id": self.telegram_chat_id,
            "input": self.input,
            "artifacts": self.artifacts,
            "error": self.error,
            "llama_stack_trace_id": self.llama_stack_trace_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<Run id={self.id} workflow={self.workflow_name} status={self.status}>"


class ArtifactType(str, Enum):
    """Type of workflow artifact."""

    IDEAS = "ideas"  # Markdown ideation for human review
    IDEAS_STRUCTURED = "ideas_structured"  # JSON structured ideation for automation
    SCRIPT = "script"
    VIDEO_CLIP = "video_clip"  # Individual clip from OpenAI Sora
    CLIP_MANIFEST = "clip_manifest"  # Task IDs submitted to OpenAI Sora
    SORA_REQUEST = "sora_request"  # Sora tool input payloads for partial retries
    PRODUCER_OUTPUT = "producer_output"  # Intermediate producer draft
    EDITOR_OUTPUT = "editor_output"  # Edited compilation before render
    PUBLISHER_OUTPUT = "publisher_output"  # Final publish package/metadata
    RENDERED_VIDEO = "rendered_video"  # Final video from Remotion
    PUBLISHED_URL = "published_url"
    VISION_ANALYSIS = "vision_analysis"  # Cached vision analysis results (analyze_media)
    ERROR = "error"
    REJECTION = "rejection"
    SAFETY_VERDICT = "safety_verdict"  # Cached safety verdicts for replay/time-travel visibility


class Artifact(Base):
    """Workflow artifact model."""

    __tablename__ = "artifacts"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id = Column(GUID(), ForeignKey("runs.id"), nullable=False)
    persona = Column(String(32), nullable=False)
    artifact_type = Column(String(64), nullable=False)
    content = Column(Text, nullable=True)
    uri = Column(String(512), nullable=True)
    artifact_metadata = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, default=_utc_now)

    run = relationship("Run", back_populates="artifacts_rel")

    def __repr__(self) -> str:
        return f"<Artifact id={self.id} run={self.run_id} type={self.artifact_type}>"


class ChatSession(Base):
    """Chat session model for multi-worker session persistence.

    Stores Llama Stack session IDs per user for the supervisor chat endpoint,
    enabling session continuity across multiple API workers.
    """

    __tablename__ = "chat_sessions"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(128), nullable=False, unique=True, index=True)
    session_id = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=_utc_now)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now)

    def __repr__(self) -> str:
        return f"<ChatSession user={self.user_id} session={self.session_id}>"


class JobStatus(str, Enum):
    """Job queue status for worker dispatch."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(Base):
    """Durable job queue entry (Postgres-backed scaling primitive).

    This table is intentionally simple:
    - API enqueues jobs
    - workers claim jobs (FOR UPDATE SKIP LOCKED) with a lease
    - jobs are retried up to max_attempts
    """

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_available_at", "status", "available_at"),
        Index("ix_jobs_run_id", "run_id"),
        Index("ix_jobs_lease_expires_at", "lease_expires_at"),
        Index("ux_jobs_type_idempotency", "job_type", "idempotency_key", unique=True),
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    job_type = Column(String(80), nullable=False)
    idempotency_key = Column(String(160), nullable=True)

    run_id = Column(GUID(), ForeignKey("runs.id"), nullable=True)
    payload = Column(JSON, default=dict)

    status = Column(String(32), nullable=False, default=JobStatus.PENDING.value)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)

    available_at = Column(DateTime, default=_utc_now, nullable=False)
    claimed_by = Column(String(128), nullable=True)
    claim_generation = Column(Integer, nullable=False, default=0)
    lease_expires_at = Column(DateTime, nullable=True)

    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utc_now)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now)

    run = relationship("Run", back_populates="jobs_rel")

    def __repr__(self) -> str:
        return (
            f"<Job id={self.id} type={self.job_type} status={self.status} "
            f"attempts={self.attempts}/{self.max_attempts}>"
        )


class AuditLog(Base):
    """System usage and decision audit trail.

    Records workflow events (start, complete, fail) and gate decisions
    for debugging, usage tracking, and compliance.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_user_id", "user_id"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(128), nullable=True)
    run_id = Column(GUID(), ForeignKey("runs.id"), nullable=True)
    action = Column(String(64), nullable=False)
    duration_ms = Column(Integer, nullable=True)
    outcome = Column(String(32), nullable=True)  # "success", "failure"
    audit_metadata = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, default=_utc_now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert audit log to dictionary."""
        return {
            "id": str(self.id),
            "user_id": self.user_id,
            "run_id": str(self.run_id) if self.run_id else None,
            "action": self.action,
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
            "metadata": self.audit_metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} action={self.action} outcome={self.outcome}>"


class Feedback(Base):
    """Human feedback on workflow outputs.

    Captures thumbs-up (5) or thumbs-down (1) ratings on run outputs,
    optionally with comments. Used for evaluation dataset curation and
    preference learning.
    """

    __tablename__ = "feedback"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id = Column(GUID(), ForeignKey("runs.id"), nullable=False, index=True)
    artifact_id = Column(GUID(), ForeignKey("artifacts.id"), nullable=True)
    rating = Column(Integer, nullable=False)  # 1 (down) or 5 (up)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utc_now)

    run = relationship("Run")
    artifact = relationship("Artifact")

    def to_dict(self) -> Dict[str, Any]:
        """Convert feedback to dictionary."""
        return {
            "id": str(self.id),
            "run_id": str(self.run_id),
            "artifact_id": str(self.artifact_id) if self.artifact_id else None,
            "rating": self.rating,
            "comment": self.comment,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return f"<Feedback id={self.id} run={self.run_id} rating={self.rating}>"


class DeadLetter(Base):
    """Dead letter queue entry for failed webhook events.

    Stores webhook events that failed after all retry attempts,
    allowing manual inspection and replay.
    """

    __tablename__ = "dead_letters"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    source = Column(String(50), nullable=False)  # "sora", "remotion"
    run_id = Column(GUID(), ForeignKey("runs.id"), nullable=False)
    payload = Column(JSON, nullable=False)  # Original webhook payload
    error = Column(Text, nullable=True)  # Error message from last attempt
    attempts = Column(Integer, default=0)  # Number of retry attempts
    created_at = Column(DateTime, default=_utc_now)
    last_attempt_at = Column(DateTime, nullable=True)  # Timestamp of last retry
    resolved_at = Column(DateTime, nullable=True)  # Timestamp when manually resolved

    run = relationship("Run")

    Index("idx_dead_letters_run_id", "run_id")
    Index("idx_dead_letters_source", "source")
    Index("idx_dead_letters_resolved", "resolved_at")

    def to_dict(self) -> Dict[str, Any]:
        """Convert dead letter to dictionary."""
        return {
            "id": str(self.id),
            "source": self.source,
            "run_id": str(self.run_id),
            "payload": self.payload,
            "error": self.error,
            "attempts": self.attempts,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }

    def __repr__(self) -> str:
        return f"<DeadLetter id={self.id} source={self.source} run={self.run_id} resolved={self.resolved_at is not None}>"
