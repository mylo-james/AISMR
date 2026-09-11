"""Data access repositories."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select, func, update, or_, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from myloware.observability.logging import get_logger
from myloware.storage.models import (
    Artifact,
    ArtifactType,
    AuditLog,
    ChatSession,
    DeadLetter,
    Feedback,
    Job,
    JobStatus,
    Run,
    RunStatus,
)

logger = get_logger(__name__)

__all__ = [
    "RunRepository",
    "ArtifactRepository",
    "ChatSessionRepository",
    "AuditLogRepository",
    "FeedbackRepository",
    "DeadLetterRepository",
    "JobRepository",
    "StaleJobClaim",
]


class StaleJobClaim(RuntimeError):
    """Raised when a worker attempts to mutate a job it no longer owns."""


class RunRepository:
    """Repository for Run CRUD operations."""

    def __init__(self, session: Session | AsyncSession):
        self.session = session

    def create(
        self,
        workflow_name: str,
        input: str,
        status: RunStatus = RunStatus.PENDING,
        current_step: str | None = None,
        vector_db_id: str | None = None,
        user_id: str | None = None,
        telegram_chat_id: str | None = None,
        public_demo: bool = False,
        public_token: str | None = None,
        public_expires_at: datetime | None = None,
    ) -> Run:
        # Support both sync and async sessions; prefer async path for AsyncSession callers
        if isinstance(self.session, AsyncSession):
            raise TypeError("Use create_async() with AsyncSession")
        from myloware.workflows.admission import require_legacy_run_creation_allowed

        require_legacy_run_creation_allowed()

        if telegram_chat_id is not None:
            try:
                telegram_chat_id = int(telegram_chat_id)
            except (TypeError, ValueError):
                telegram_chat_id = None

        run = Run(
            workflow_name=workflow_name,
            vector_db_id=vector_db_id,
            input=input,
            status=status.value,
            current_step=current_step,
            user_id=user_id,
            telegram_chat_id=telegram_chat_id,
            public_demo=public_demo,
            public_token=public_token,
            public_expires_at=public_expires_at,
        )
        self.session.add(run)
        self.session.flush()
        logger.info("Created run %s for workflow %s", run.id, workflow_name)
        return run

    async def create_async(
        self,
        workflow_name: str,
        input: str,
        status: RunStatus = RunStatus.PENDING,
        current_step: str | None = None,
        vector_db_id: str | None = None,
        user_id: str | None = None,
        telegram_chat_id: str | None = None,
        public_demo: bool = False,
        public_token: str | None = None,
        public_expires_at: datetime | None = None,
    ) -> Run:
        """Create a run using an AsyncSession."""
        if not isinstance(self.session, AsyncSession):
            raise TypeError("Use create() with Session")
        from myloware.workflows.admission import require_legacy_run_creation_allowed

        require_legacy_run_creation_allowed()
        # Normalize telegram_chat_id to int to match DB column (bigint)
        if telegram_chat_id is not None:
            try:
                telegram_chat_id = int(telegram_chat_id)
            except (TypeError, ValueError):
                telegram_chat_id = None

        run = Run(
            workflow_name=workflow_name,
            vector_db_id=vector_db_id,
            input=input,
            status=status.value,
            current_step=current_step,
            user_id=user_id,
            telegram_chat_id=telegram_chat_id,
            public_demo=public_demo,
            public_token=public_token,
            public_expires_at=public_expires_at,
        )
        self.session.add(run)
        await self.session.flush()
        logger.info("Created run %s for workflow %s (async)", run.id, workflow_name)
        return run

    def get(self, run_id: UUID) -> Optional[Run]:
        return self.session.query(Run).filter(Run.id == run_id).first()

    async def get_async(self, run_id: UUID) -> Optional[Run]:
        result = await self.session.execute(select(Run).where(Run.id == run_id))
        return result.scalar_one_or_none()

    async def get_by_public_token_async(self, token: str) -> Optional[Run]:
        result = await self.session.execute(select(Run).where(Run.public_token == token))
        return result.scalar_one_or_none()

    def get_for_update(self, run_id: UUID) -> Optional[Run]:
        """Get a run with a FOR UPDATE lock to prevent race conditions.

        Use this when you need to read-modify-write a run atomically,
        e.g., in webhook handlers where multiple callbacks may arrive concurrently.
        """
        stmt = select(Run).where(Run.id == run_id).with_for_update()
        return self.session.execute(stmt).scalar_one_or_none()

    async def get_for_update_async(self, run_id: UUID) -> Optional[Run]:
        stmt = select(Run).where(Run.id == run_id).with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    def get_by_id_str(self, run_id: str) -> Optional[Run]:
        return self.get(UUID(run_id))

    def update(self, run_id: UUID, **kwargs: Any) -> Optional[Run]:
        run = self.get(run_id)
        if run is None:
            return None

        for key, value in kwargs.items():
            if hasattr(run, key):
                if key == "status":
                    value = self._normalize_status(value)
                setattr(run, key, value)

        self.session.flush()
        logger.info("Updated run %s: %s", run_id, list(kwargs.keys()))
        return run

    async def update_async(self, run_id: UUID, **kwargs: Any) -> Optional[Run]:
        run = await self.get_async(run_id)
        if run is None:
            return None

        for key, value in kwargs.items():
            if hasattr(run, key):
                if key == "status":
                    value = self._normalize_status(value)
                setattr(run, key, value)

        await self.session.flush()
        logger.info("Updated run %s async: %s", run_id, list(kwargs.keys()))
        return run

    def update_status(self, run_id: UUID, status: RunStatus) -> Optional[Run]:
        return self.update(run_id, status=status.value)

    async def update_status_async(self, run_id: UUID, status: RunStatus) -> Optional[Run]:
        return await self.update_async(run_id, status=status.value)

    async def count_runs_since_async(self, dt: datetime) -> int:
        # Normalize to naive datetime because created_at is stored without tz
        dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
        stmt = select(func.count()).select_from(Run).where(Run.created_at >= dt_naive)
        result = await self.session.execute(stmt)
        return int(result.scalar_one() or 0)

    @staticmethod
    def _normalize_status(value: RunStatus | str) -> str:
        """Validate and normalize run status to string value."""
        if isinstance(value, RunStatus):
            return value.value
        if isinstance(value, str):
            try:
                return RunStatus(value).value
            except ValueError:
                # Be lenient about casing for callers that pass e.g. "FAILED".
                lowered = value.lower()
                if lowered in {status.value for status in RunStatus}:
                    return lowered
        raise ValueError(f"Invalid run status: {value}")

    def update_step(self, run_id: UUID, step: str) -> Optional[Run]:
        return self.update(run_id, current_step=step)

    def add_artifact(self, run_id: UUID, key: str, value: Any) -> Optional[Run]:
        run = self.get(run_id)
        if run is None:
            return None

        artifacts = run.artifacts_dict.copy()
        artifacts[key] = value
        run.artifacts = artifacts
        # Explicitly mark JSON column as modified (SQLAlchemy may not detect mutations)
        flag_modified(run, "artifacts")
        self.session.flush()
        logger.info("Added artifact '%s' to run %s", key, run_id)
        return run

    async def add_artifact_async(self, run_id: UUID, key: str, value: Any) -> Optional[Run]:
        run = await self.get_async(run_id)
        if run is None:
            return None

        artifacts = run.artifacts_dict.copy()
        artifacts[key] = value
        run.artifacts = artifacts
        flag_modified(run, "artifacts")
        await self.session.flush()
        logger.info("Added artifact '%s' to run %s (async)", key, run_id)
        return run

    def list(
        self,
        limit: int = 10,
        offset: int = 0,
        status: Optional[RunStatus] = None,
        user_id: str | None = None,
    ) -> List[Run]:
        query = self.session.query(Run)
        if status:
            query = query.filter(Run.status == status.value)
        if user_id:
            query = query.filter(Run.user_id == user_id)
        return query.order_by(Run.created_at.desc()).offset(offset).limit(limit).all()

    def find_by_status_and_age(
        self,
        status: str,
        older_than: datetime,
    ) -> List[Run]:
        """Find runs with a specific status that haven't been updated since cutoff.

        Args:
            status: Run status value to filter by
            older_than: Cutoff datetime - returns runs updated before this time

        Returns:
            List of Run objects matching the criteria
        """
        return (
            self.session.query(Run)
            .filter(Run.status == status)
            .filter(Run.updated_at < older_than)
            .order_by(Run.updated_at.asc())
            .all()
        )

    async def find_by_status_and_age_async(
        self,
        status: str,
        older_than: datetime,
    ) -> List[Run]:
        """Async: Find runs with a specific status that haven't been updated since cutoff."""
        stmt = (
            select(Run)
            .where(Run.status == status)
            .where(Run.updated_at < older_than)
            .order_by(Run.updated_at.asc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()


class ArtifactRepository:
    """Repository for Artifact CRUD operations."""

    def __init__(self, session: Session | AsyncSession):
        self.session = session

    def create(
        self,
        run_id: UUID,
        persona: str,
        artifact_type: ArtifactType,
        content: Optional[str] = None,
        uri: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ) -> Artifact:
        """Create a new artifact."""

        meta = metadata.copy() if metadata else {}
        if trace_id:
            meta.setdefault("trace_id", trace_id)

        artifact = Artifact(
            run_id=run_id,
            persona=persona,
            artifact_type=artifact_type.value,
            content=content,
            uri=uri,
            artifact_metadata=meta,
        )
        self.session.add(artifact)
        self.session.flush()
        logger.info(
            "Created artifact type=%s persona=%s run=%s", artifact_type.value, persona, run_id
        )
        return artifact

    async def create_async(
        self,
        run_id: UUID,
        persona: str,
        artifact_type: ArtifactType,
        content: Optional[str] = None,
        uri: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ) -> Artifact:
        meta = metadata.copy() if metadata else {}
        if trace_id:
            meta.setdefault("trace_id", trace_id)

        artifact = Artifact(
            run_id=run_id,
            persona=persona,
            artifact_type=artifact_type.value,
            content=content,
            uri=uri,
            artifact_metadata=meta,
        )
        self.session.add(artifact)
        await self.session.flush()
        logger.info(
            "Created artifact type=%s persona=%s run=%s (async)",
            artifact_type.value,
            persona,
            run_id,
        )
        return artifact

    def get_by_run(self, run_id: UUID) -> List[Artifact]:
        """Get all artifacts for a run ordered by creation time."""
        return (
            self.session.query(Artifact)
            .filter(Artifact.run_id == run_id)
            .order_by(Artifact.created_at)
            .all()
        )

    async def get_by_run_async(self, run_id: UUID) -> List[Artifact]:
        """Async: Get all artifacts for a run ordered by creation time."""
        result = await self.session.execute(
            select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
        )
        return result.scalars().all()

    def get_by_type(self, run_id: UUID, artifact_type: ArtifactType) -> Optional[Artifact]:
        """Get a single artifact by type for a run."""
        return (
            self.session.query(Artifact)
            .filter(Artifact.run_id == run_id)
            .filter(Artifact.artifact_type == artifact_type.value)
            .first()
        )

    def get_latest_artifact_by_type(
        self, run_id: UUID, artifact_type: ArtifactType
    ) -> Optional[Artifact]:
        """Get the most recent artifact of a specific type for a run."""
        return (
            self.session.query(Artifact)
            .filter(Artifact.run_id == run_id)
            .filter(Artifact.artifact_type == artifact_type.value)
            .order_by(Artifact.created_at.desc())
            .first()
        )

    async def get_latest_artifact_by_type_async(
        self, run_id: UUID, artifact_type: ArtifactType
    ) -> Optional[Artifact]:
        if not isinstance(self.session, AsyncSession):
            raise TypeError("Use get_latest_artifact_by_type() with Session")
        result = await self.session.execute(
            select(Artifact)
            .where(Artifact.run_id == run_id)
            .where(Artifact.artifact_type == artifact_type.value)
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    def find_cached_videos(
        self,
        topic: str,
        signs: Optional[List[str]] = None,
        limit: int = 12,
    ) -> List[Artifact]:
        """Find cached video clips by topic and optional zodiac signs.

        Searches across ALL runs for VIDEO_CLIP artifacts with matching metadata.
        When signs are provided, returns ONE clip per sign in the order specified.

        Args:
            topic: The topic to match (e.g., "puppies")
            signs: Optional list of zodiac signs - results will be ordered by this list
            limit: Maximum number of results to return

        Returns:
            List of matching Artifact objects with video URLs, ordered by signs if provided
        """
        # Get all video clips with URIs first, then filter in Python
        # This avoids JSON query compatibility issues across PostgreSQL/SQLite
        all_clips = (
            self.session.query(Artifact)
            .filter(Artifact.artifact_type == ArtifactType.VIDEO_CLIP.value)
            .filter(Artifact.uri.isnot(None))
            .order_by(Artifact.created_at.desc())
            .all()
        )

        # Build a dict of sign -> clip (most recent first, so first match wins)
        sign_to_clip: Dict[str, Artifact] = {}
        topic_clips: List[Artifact] = []

        for clip in all_clips:
            meta = clip.artifact_metadata or {}
            clip_topic = meta.get("topic", "")
            clip_sign = meta.get("sign", "")

            if clip_topic.lower() == topic.lower():
                if signs is None:
                    # No sign filter - just collect all
                    topic_clips.append(clip)
                    if len(topic_clips) >= limit:
                        break
                elif clip_sign and clip_sign in signs and clip_sign not in sign_to_clip:
                    # Sign filter - store first (most recent) clip per sign
                    sign_to_clip[clip_sign] = clip

        if signs is None:
            return topic_clips

        # Return clips in the order of signs list
        results = []
        for sign in signs:
            if sign in sign_to_clip:
                results.append(sign_to_clip[sign])
            if len(results) >= limit:
                break

        return results

    async def find_run_for_sora_task_async(
        self,
        task_id: str,
    ) -> tuple[UUID, dict[str, Any]] | None:
        """Find the run + stored metadata for a given Sora/OpenAI video task id.

        OpenAI video webhooks do not include our run_id. We persist a per-run
        CLIP_MANIFEST artifact that maps task_id -> metadata; this helper scans
        those manifests to recover the owning run.

        Returns:
            (run_id, metadata) if found, else None.
        """
        import json

        if not isinstance(self.session, AsyncSession):
            raise TypeError("find_run_for_sora_task_async requires an AsyncSession")

        task_id = (task_id or "").strip()
        if not task_id:
            return None

        needle = f'"{task_id}"'

        # Portable search: use LIKE on content to narrow candidates, then JSON-parse.
        query = (
            select(Artifact)
            .where(Artifact.artifact_type == ArtifactType.CLIP_MANIFEST.value)
            .where(Artifact.content.isnot(None))
            .where(Artifact.content.contains(needle))
            .order_by(Artifact.created_at.desc())
        )
        result = await self.session.execute(query)

        for artifact in result.scalars().all():
            meta = artifact.artifact_metadata or {}
            if meta.get("type") != "task_metadata_mapping":
                continue
            mapping = None
            try:
                mapping = json.loads(artifact.content or "{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                mapping = None
            if not isinstance(mapping, dict):
                continue
            task_meta = mapping.get(task_id)
            if isinstance(task_meta, dict):
                return (UUID(str(artifact.run_id)), task_meta)

        return None

    def count_cached_videos_by_topic(self, topic: str) -> int:
        """Count how many cached videos exist for a topic."""
        all_clips = (
            self.session.query(Artifact)
            .filter(Artifact.artifact_type == ArtifactType.VIDEO_CLIP.value)
            .filter(Artifact.uri.isnot(None))
            .all()
        )

        count = 0
        for clip in all_clips:
            meta = clip.artifact_metadata or {}
            if meta.get("topic", "").lower() == topic.lower():
                count += 1
        return count


class ChatSessionRepository:
    """Repository for ChatSession CRUD operations.

    Provides database-backed session storage for multi-worker deployments,
    replacing the in-memory session cache.
    """

    def __init__(self, session: Session):
        self.session = session

    def get_session(self, user_id: str) -> Optional[str]:
        """Get Llama Stack session ID for a user."""
        chat_session = (
            self.session.query(ChatSession).filter(ChatSession.user_id == user_id).first()
        )
        return chat_session.session_id if chat_session else None

    def create_or_update_session(self, user_id: str, session_id: str) -> ChatSession:
        """Create or update a chat session for a user."""
        chat_session = (
            self.session.query(ChatSession).filter(ChatSession.user_id == user_id).first()
        )
        if chat_session:
            chat_session.session_id = session_id
            logger.info("Updated chat session for user %s", user_id)
        else:
            chat_session = ChatSession(user_id=user_id, session_id=session_id)
            self.session.add(chat_session)
            logger.info("Created chat session for user %s", user_id)
        self.session.flush()
        return chat_session

    def delete_session(self, user_id: str) -> bool:
        """Delete a chat session for a user."""
        deleted = self.session.query(ChatSession).filter(ChatSession.user_id == user_id).delete()
        self.session.flush()
        return deleted > 0


class AuditLogRepository:
    """Repository for AuditLog CRUD operations.

    Provides audit trail storage for workflow events and gate decisions.
    """

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        action: str,
        user_id: Optional[str] = None,
        run_id: Optional[UUID] = None,
        duration_ms: Optional[int] = None,
        outcome: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AuditLog:
        """Create an audit log entry."""
        audit_log = AuditLog(
            action=action,
            user_id=user_id,
            run_id=run_id,
            duration_ms=duration_ms,
            outcome=outcome,
            audit_metadata=metadata or {},
        )
        self.session.add(audit_log)
        self.session.flush()
        logger.info(
            "audit_log_created",
            action=action,
            user_id=user_id,
            run_id=str(run_id) if run_id else None,
            outcome=outcome,
        )
        return audit_log

    def get_by_run_id(self, run_id: UUID) -> List[AuditLog]:
        """Get all audit logs for a run ordered by creation time."""
        return (
            self.session.query(AuditLog)
            .filter(AuditLog.run_id == run_id)
            .order_by(AuditLog.created_at)
            .all()
        )

    def get_by_user_id(self, user_id: str, limit: int = 100) -> List[AuditLog]:
        """Get audit logs for a user ordered by creation time (most recent first)."""
        return (
            self.session.query(AuditLog)
            .filter(AuditLog.user_id == user_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .all()
        )


class FeedbackRepository:
    """Repository for Feedback CRUD operations.

    Manages human feedback on workflow outputs for evaluation
    and preference learning.
    """

    def __init__(self, session: Session | AsyncSession):
        self.session = session

    def create(
        self,
        run_id: UUID,
        rating: int,
        artifact_id: Optional[UUID] = None,
        comment: Optional[str] = None,
    ) -> Feedback:
        """Create a feedback entry.

        Args:
            run_id: ID of the run being rated
            rating: Rating value (1=down, 5=up)
            artifact_id: Optional specific artifact being rated
            comment: Optional text comment

        Returns:
            Created Feedback instance
        """
        feedback = Feedback(
            run_id=run_id,
            artifact_id=artifact_id,
            rating=rating,
            comment=comment,
        )
        self.session.add(feedback)
        self.session.flush()
        logger.info(
            "feedback_created",
            run_id=str(run_id),
            rating=rating,
        )
        return feedback

    async def create_async(
        self,
        run_id: UUID,
        rating: int,
        artifact_id: Optional[UUID] = None,
        comment: Optional[str] = None,
    ) -> Feedback:
        feedback = Feedback(
            run_id=run_id,
            artifact_id=artifact_id,
            rating=rating,
            comment=comment,
        )
        self.session.add(feedback)
        await self.session.flush()
        logger.info(
            "feedback_created_async",
            run_id=str(run_id),
            rating=rating,
        )
        return feedback

    def get_by_run_id(self, run_id: UUID) -> List[Feedback]:
        """Get all feedback for a run."""
        return (
            self.session.query(Feedback)
            .filter(Feedback.run_id == run_id)
            .order_by(Feedback.created_at.desc())
            .all()
        )

    async def get_by_run_id_async(self, run_id: UUID) -> List[Feedback]:
        """Async: Get all feedback for a run."""
        result = await self.session.execute(
            select(Feedback).where(Feedback.run_id == run_id).order_by(Feedback.created_at.desc())
        )
        return result.scalars().all()

    def get_positive_feedback(self, limit: int = 100) -> List[Feedback]:
        """Get positive feedback entries for eval dataset curation.

        Returns feedback with rating >= 4, ordered by most recent.
        """
        return (
            self.session.query(Feedback)
            .filter(Feedback.rating >= 4)
            .order_by(Feedback.created_at.desc())
            .limit(limit)
            .all()
        )


class DeadLetterRepository:
    """Repository for DeadLetter CRUD operations."""

    def __init__(self, session: Session | AsyncSession):
        self.session = session

    def create(
        self,
        source: str,
        run_id: UUID,
        payload: Dict[str, Any],
        error: str | None = None,
        attempts: int = 0,
    ) -> DeadLetter:
        """Create a dead letter entry.

        Args:
            source: Source of the webhook ("sora", "remotion")
            run_id: ID of the run associated with the webhook
            payload: Original webhook payload
            error: Error message from last attempt
            attempts: Number of retry attempts made

        Returns:
            Created DeadLetter instance
        """
        from datetime import timezone

        dead_letter = DeadLetter(
            source=source,
            run_id=run_id,
            payload=payload,
            error=error,
            attempts=attempts,
            last_attempt_at=datetime.now(timezone.utc),
        )
        self.session.add(dead_letter)
        self.session.flush()
        logger.info(
            "dead_letter_created",
            id=str(dead_letter.id),
            source=source,
            run_id=str(run_id),
            attempts=attempts,
        )
        return dead_letter

    async def create_async(
        self,
        source: str,
        run_id: UUID,
        payload: Dict[str, Any],
        error: str | None = None,
        attempts: int = 0,
    ) -> DeadLetter:
        """Async: Create a dead letter entry."""
        from datetime import timezone

        dead_letter = DeadLetter(
            source=source,
            run_id=run_id,
            payload=payload,
            error=error,
            attempts=attempts,
            last_attempt_at=datetime.now(timezone.utc),
        )
        self.session.add(dead_letter)
        await self.session.flush()
        logger.info(
            "dead_letter_created_async",
            id=str(dead_letter.id),
            source=source,
            run_id=str(run_id),
        )
        return dead_letter

    def get_unresolved(self, source: str | None = None) -> List[DeadLetter]:
        """Get all unresolved dead letters, optionally filtered by source.

        Args:
            source: Optional source filter ("sora", "remotion")

        Returns:
            List of unresolved DeadLetter entries
        """
        query = self.session.query(DeadLetter).filter(DeadLetter.resolved_at.is_(None))
        if source:
            query = query.filter(DeadLetter.source == source)
        return query.order_by(DeadLetter.created_at.desc()).all()

    async def get_unresolved_async(self, source: str | None = None) -> List[DeadLetter]:
        """Async: Get all unresolved dead letters."""
        query = select(DeadLetter).where(DeadLetter.resolved_at.is_(None))
        if source:
            query = query.where(DeadLetter.source == source)
        query = query.order_by(DeadLetter.created_at.desc())
        result = await self.session.execute(query)
        return result.scalars().all()

    def get(self, dead_letter_id: UUID) -> Optional[DeadLetter]:
        """Get a dead letter by ID."""
        return self.session.query(DeadLetter).filter(DeadLetter.id == dead_letter_id).first()

    async def get_async(self, dead_letter_id: UUID) -> Optional[DeadLetter]:
        """Async: Get a dead letter by ID."""
        result = await self.session.execute(
            select(DeadLetter).where(DeadLetter.id == dead_letter_id)
        )
        return result.scalar_one_or_none()

    def mark_resolved(self, dead_letter_id: UUID) -> None:
        """Mark a dead letter as resolved."""
        from datetime import timezone

        dead_letter = self.get(dead_letter_id)
        if dead_letter:
            dead_letter.resolved_at = datetime.now(timezone.utc)
            self.session.flush()
            logger.info("dead_letter_resolved", id=str(dead_letter_id))

    async def mark_resolved_async(self, dead_letter_id: UUID) -> None:
        """Async: Mark a dead letter as resolved."""
        from datetime import timezone

        dead_letter = await self.get_async(dead_letter_id)
        if dead_letter:
            dead_letter.resolved_at = datetime.now(timezone.utc)
            await self.session.flush()
            logger.info("dead_letter_resolved_async", id=str(dead_letter_id))

    def increment_attempts(self, dead_letter_id: UUID) -> None:
        """Increment retry attempts counter."""
        from datetime import timezone

        dead_letter = self.get(dead_letter_id)
        if dead_letter:
            dead_letter.attempts += 1
            dead_letter.last_attempt_at = datetime.now(timezone.utc)
            self.session.flush()

    async def increment_attempts_async(self, dead_letter_id: UUID) -> None:
        """Async: Increment retry attempts counter."""
        from datetime import timezone

        dead_letter = await self.get_async(dead_letter_id)
        if dead_letter:
            dead_letter.attempts += 1
            dead_letter.last_attempt_at = datetime.now(timezone.utc)
            await self.session.flush()


class JobRepository:
    """Repository for durable job queue operations.

    The job queue is Postgres-first (FOR UPDATE SKIP LOCKED), with a portable
    fallback for SQLite used in tests.
    """

    def __init__(self, session: Session | AsyncSession):
        self.session = session

    @staticmethod
    def _utc_now_naive() -> datetime:
        # Keep timestamps tz-naive to match the DB convention in models.py
        from datetime import timezone

        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _dialect_name(self) -> str:
        bind = getattr(self.session, "bind", None)
        if bind is None and hasattr(self.session, "get_bind"):
            try:
                bind = self.session.get_bind()
            except Exception:
                bind = None
        if bind is None:
            return "unknown"
        if hasattr(bind, "dialect"):
            return str(bind.dialect.name)
        if hasattr(bind, "sync_engine") and hasattr(bind.sync_engine, "dialect"):
            return str(bind.sync_engine.dialect.name)
        return "unknown"

    async def enqueue_async(
        self,
        job_type: str,
        *,
        run_id: UUID | None = None,
        payload: Dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 5,
        available_at: datetime | None = None,
    ) -> Job:
        if not isinstance(self.session, AsyncSession):
            raise TypeError("enqueue_async requires an AsyncSession")
        job = Job(
            job_type=job_type,
            run_id=run_id,
            payload=payload or {},
            idempotency_key=idempotency_key,
            status=JobStatus.PENDING.value,
            max_attempts=max_attempts,
            available_at=available_at or self._utc_now_naive(),
        )
        self.session.add(job)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            # Re-raise with a stable error for callers to treat as "already enqueued".
            raise ValueError("job_already_enqueued") from exc
        return job

    async def claim_next_async(
        self,
        *,
        worker_id: str,
        lease_seconds: float = 60.0,
        run_id: UUID | None = None,
        job_types: tuple[str, ...] | None = None,
    ) -> Job | None:
        """Claim the next available job.

        Uses SKIP LOCKED on Postgres; falls back to optimistic claim on SQLite.
        """
        if not isinstance(self.session, AsyncSession):
            raise TypeError("claim_next_async requires an AsyncSession")
        now = self._utc_now_naive()
        lease_expires_at = now + timedelta(seconds=float(lease_seconds))

        eligible = or_(
            and_(
                Job.status == JobStatus.PENDING.value,
                Job.available_at <= now,
                or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now),
            ),
            and_(Job.status == JobStatus.RUNNING.value, Job.lease_expires_at <= now),
        )

        if run_id is not None:
            eligible = and_(eligible, Job.run_id == run_id)
        if job_types is not None:
            eligible = and_(eligible, Job.job_type.in_(job_types))

        dialect = self._dialect_name()
        if dialect == "postgresql":
            stmt = (
                select(Job)
                .where(
                    eligible,
                )
                .order_by(Job.available_at.asc(), Job.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            result = await self.session.execute(stmt)
            job = result.scalar_one_or_none()
            if job is None:
                return None
            job.status = JobStatus.RUNNING.value
            job.claimed_by = worker_id
            job.lease_expires_at = lease_expires_at
            job.attempts = int(job.attempts or 0) + 1
            job.claim_generation = int(job.claim_generation or 0) + 1
            await self.session.flush()
            return job

        # Portable fallback: select then conditionally update.
        stmt = (
            select(Job.id)
            .where(eligible)
            .order_by(Job.available_at.asc(), Job.created_at.asc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        job_id = result.scalar_one_or_none()
        if job_id is None:
            return None

        upd = (
            update(Job)
            .where(
                Job.id == job_id,
                eligible,
            )
            .values(
                status=JobStatus.RUNNING.value,
                claimed_by=worker_id,
                lease_expires_at=lease_expires_at,
                attempts=Job.attempts + 1,
                claim_generation=Job.claim_generation + 1,
            )
        )
        res = await self.session.execute(upd)
        if not getattr(res, "rowcount", 0):
            return None
        # Ensure we return refreshed state even if the Job is already present
        # in the session identity map (SQLite fallback uses UPDATE statements).
        return await self.session.get(Job, job_id, populate_existing=True)

    async def touch_lease_async(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        claim_generation: int,
        lease_seconds: float = 60.0,
    ) -> bool:
        if not isinstance(self.session, AsyncSession):
            raise TypeError("touch_lease_async requires an AsyncSession")
        now = self._utc_now_naive()
        lease_expires_at = now + timedelta(seconds=float(lease_seconds))
        upd = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING.value,
                Job.claimed_by == worker_id,
                Job.claim_generation == claim_generation,
            )
            .values(lease_expires_at=lease_expires_at, updated_at=now)
        )
        result = await self.session.execute(upd)
        await self.session.flush()
        if not getattr(result, "rowcount", 0):
            raise StaleJobClaim(f"stale claim for job {job_id}")
        return True

    async def get_current_claim_async(
        self, job_id: UUID, *, worker_id: str, claim_generation: int
    ) -> Job | None:
        """Return a job only while this exact worker claim remains current."""
        if not isinstance(self.session, AsyncSession):
            raise TypeError("get_current_claim_async requires an AsyncSession")
        result = await self.session.execute(
            select(Job).where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING.value,
                Job.claimed_by == worker_id,
                Job.claim_generation == claim_generation,
            )
        )
        return result.scalar_one_or_none()

    async def mark_succeeded_async(
        self, job_id: UUID, *, worker_id: str, claim_generation: int
    ) -> bool:
        if not isinstance(self.session, AsyncSession):
            raise TypeError("mark_succeeded_async requires an AsyncSession")
        now = self._utc_now_naive()
        upd = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING.value,
                Job.claimed_by == worker_id,
                Job.claim_generation == claim_generation,
            )
            .values(
                status=JobStatus.SUCCEEDED.value,
                lease_expires_at=None,
                claimed_by=None,
                updated_at=now,
            )
        )
        result = await self.session.execute(upd)
        await self.session.flush()
        if not getattr(result, "rowcount", 0):
            raise StaleJobClaim(f"stale claim for job {job_id}")
        return True

    async def mark_failed_async(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        claim_generation: int,
        error: str,
        retry_delay_seconds: float = 5.0,
    ) -> JobStatus:
        """Mark job failed; optionally reschedule if attempts remain.

        Returns the resulting JobStatus.
        """
        if not isinstance(self.session, AsyncSession):
            raise TypeError("mark_failed_async requires an AsyncSession")

        job = await self.get_current_claim_async(
            job_id, worker_id=worker_id, claim_generation=claim_generation
        )
        if job is None:
            raise StaleJobClaim(f"stale claim for job {job_id}")

        if int(job.attempts or 0) >= int(job.max_attempts or 0):
            status = JobStatus.FAILED
            available_at = job.available_at
        else:
            status = JobStatus.PENDING
            available_at = self._utc_now_naive() + timedelta(seconds=float(retry_delay_seconds))

        now = self._utc_now_naive()
        upd = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING.value,
                Job.claimed_by == worker_id,
                Job.claim_generation == claim_generation,
            )
            .values(
                status=status.value,
                last_error=error,
                lease_expires_at=None,
                claimed_by=None,
                available_at=available_at,
                updated_at=now,
            )
        )
        result = await self.session.execute(upd)
        await self.session.flush()
        if not getattr(result, "rowcount", 0):
            raise StaleJobClaim(f"stale claim for job {job_id}")
        if status is JobStatus.FAILED:
            return JobStatus.FAILED
        return JobStatus.PENDING
