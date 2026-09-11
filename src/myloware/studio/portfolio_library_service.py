"""Dedicated, bounded public-library projection for AISMR finals.

This service never opens a source-mode database.  Callers validate visitor
authority and create the artifact-bound consent receipt before passing a source
final here; the returned mapping is the seam used for post-cleanup playback.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from myloware.storage.models import _utc_now
from myloware.storage.studio_models import (
    PortfolioLibraryBase,
    StudioGalleryEntry,
    StudioGalleryLock,
    StudioGalleryOutbox,
    StudioGalleryProjectionIntent,
)
from myloware.studio.portfolio_library import PortfolioLibraryFiles
from myloware.workers.claims import require_current_claim


class PortfolioLibraryServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublicFinal:
    source_instance_key: str
    source_run_id: UUID
    source_revision: int
    source_mode: str
    item_label: str
    source_final: Path
    final_sha256: str
    accepted_at: datetime
    public_suitability_receipt: str
    rights_receipt: str
    consent_subject_hash: str
    history: dict[str, object]


@dataclass(frozen=True)
class PortfolioEntry:
    id: UUID
    source_instance_key: str
    item_label: str
    source_mode: str
    final_sha256: str
    accepted_at: datetime
    serving_key: str
    history: dict[str, object]


@dataclass(frozen=True)
class SourceGalleryConsent:
    """The exact source-transaction data required for a public projection.

    ``public_suitability_receipt`` and ``rights_receipt`` must name successful,
    current source-side checks for this exact final hash.  Fixture/demo modes,
    historical approval, or a generic final-review acceptance never satisfy
    this contract.
    """

    run_id: UUID
    revision: int
    final_hash: str
    decision_key: str
    consent_subject_hash: str
    source_mode: str
    item_label: str
    public_suitability_receipt: str
    rights_receipt: str
    history: dict[str, object]


async def record_gallery_projection_intent(
    session: AsyncSession, *, consent: SourceGalleryConsent
) -> StudioGalleryProjectionIntent:
    """Add a source outbox row inside the final-consent transaction.

    Call this after the source store has proved the exact final-review decision
    and public-gallery opt-in, but before that transaction commits.  The caller
    must also prove the suitability and rights receipts are successful and bind
    them to this final hash.  This helper rejects fixture/demo source modes.
    """
    PortfolioLibraryService.validate_source_consent(consent)
    source_key = f"{consent.source_mode}:{consent.run_id}:{consent.revision}:{consent.final_hash}"
    existing = await session.scalar(
        select(StudioGalleryProjectionIntent).where(
            StudioGalleryProjectionIntent.run_id == consent.run_id,
            StudioGalleryProjectionIntent.revision == consent.revision,
            StudioGalleryProjectionIntent.final_hash == consent.final_hash,
            StudioGalleryProjectionIntent.decision_key == consent.decision_key,
        )
    )
    if existing is not None:
        if (
            existing.consent_subject_hash != consent.consent_subject_hash
            or existing.public_suitability_receipt != consent.public_suitability_receipt
            or existing.rights_receipt != consent.rights_receipt
        ):
            raise PortfolioLibraryServiceError("portfolio source consent conflicts")
        return existing
    intent = StudioGalleryProjectionIntent(
        run_id=consent.run_id,
        revision=consent.revision,
        final_hash=consent.final_hash,
        decision_key=consent.decision_key,
        consent_subject_hash=consent.consent_subject_hash,
        source_instance_key=source_key,
        source_mode=consent.source_mode,
        item_label=consent.item_label,
        public_suitability_receipt=consent.public_suitability_receipt,
        rights_receipt=consent.rights_receipt,
        history=PortfolioLibraryService._sanitize_history(consent.history),
    )
    session.add(intent)
    await session.flush()
    return intent


class PortfolioLibraryService:
    """Copy, activate, retire, serve and reconcile at most three public finals."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        media_root: Path,
        protected_roots: tuple[Path, ...] = (),
        source_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.factory = factory
        self.source_factory = source_factory
        self.files = PortfolioLibraryFiles(media_root, forbidden_roots=protected_roots)

    async def initialize(self) -> None:
        async with self.factory() as session:
            bind = session.get_bind()
            if bind.dialect.name not in {"sqlite", "postgresql"}:
                raise PortfolioLibraryServiceError("portfolio library database is unsupported")
            async with session.begin():
                connection = await session.connection()
                await connection.run_sync(PortfolioLibraryBase.metadata.create_all)
                lock = await session.get(StudioGalleryLock, 1)
                if lock is None:
                    session.add(StudioGalleryLock(id=1, generation=0))

    async def accept(self, final: PublicFinal) -> PortfolioEntry:
        self._validate_final(final)
        entry = await self._prepare(final)
        # Source bytes are copied and hashed before the public state can become active.
        await asyncio.to_thread(
            self.files.copy_verified, final.source_final, entry.serving_key, final.final_sha256
        )
        return await self._activate(entry.id)

    async def project_source_intent(self, intent_id: UUID, *, source_final: Path) -> PortfolioEntry:
        """Claim and acknowledge a committed source outbox intent.

        The existing job claim fences both source writes.  A crash after copy or
        library activation leaves the source intent unacknowledged, so the next
        claimed worker safely reuses the same source-instance key and serving
        copy before recording the library entry id.
        """
        if self.source_factory is None:
            raise PortfolioLibraryServiceError("source intent factory is required")
        async with self.source_factory() as session, session.begin():
            await require_current_claim(session)
            intent = await session.get(StudioGalleryProjectionIntent, intent_id)
            if intent is None:
                raise PortfolioLibraryServiceError("source gallery intent is missing")
            if intent.state == "complete" and intent.library_entry_id is not None:
                entry = await self.entry_for_source(intent.source_instance_key)
                if entry is None:
                    raise PortfolioLibraryServiceError("acknowledged library entry is unavailable")
                return entry
            if intent.state not in {"pending", "projecting"}:
                raise PortfolioLibraryServiceError("source gallery intent is not projectable")
            intent.state = "projecting"
            intent.claim_generation += 1
            final = PublicFinal(
                source_instance_key=intent.source_instance_key,
                source_run_id=intent.run_id,
                source_revision=intent.revision,
                source_mode=intent.source_mode,
                item_label=intent.item_label,
                source_final=source_final,
                final_sha256=intent.final_hash,
                accepted_at=intent.created_at,
                public_suitability_receipt=intent.public_suitability_receipt,
                rights_receipt=intent.rights_receipt,
                consent_subject_hash=intent.consent_subject_hash,
                history=dict(intent.history or {}),
            )
        entry = await self.accept(final)
        await self._ack_source_intent(intent_id, entry)
        return entry

    async def _ack_source_intent(self, intent_id: UUID, entry: PortfolioEntry) -> None:
        if self.source_factory is None:
            raise PortfolioLibraryServiceError("source intent factory is required")
        async with self.source_factory() as session, session.begin():
            await require_current_claim(session)
            intent = await session.get(StudioGalleryProjectionIntent, intent_id)
            if intent is None:
                raise PortfolioLibraryServiceError("source gallery intent is missing")
            if intent.state == "complete":
                if intent.library_entry_id != entry.id:
                    raise PortfolioLibraryServiceError("source gallery acknowledgement conflicts")
                return
            if (
                intent.final_hash != entry.final_sha256
                or intent.source_instance_key != entry.source_instance_key
            ):
                raise PortfolioLibraryServiceError("source gallery acknowledgement is stale")
            intent.library_entry_id = entry.id
            intent.state = "complete"
            intent.completed_at = _utc_now()

    async def gallery(self) -> tuple[PortfolioEntry, ...]:
        async with self.factory() as session:
            rows = (await session.scalars(self._active_statement())).all()
        result: list[PortfolioEntry] = []
        for row in rows:
            if self._current(row.history) and await asyncio.to_thread(
                self.files.verified, row.serving_key, row.final_sha256
            ):
                result.append(self._project(row))
        return tuple(result)

    async def entry_for_source(self, source_instance_key: str) -> PortfolioEntry | None:
        async with self.factory() as session:
            row = await session.scalar(
                select(StudioGalleryEntry).where(
                    StudioGalleryEntry.source_instance_key == source_instance_key,
                    StudioGalleryEntry.state == "active",
                )
            )
        if (
            row is None
            or not self._current(row.history)
            or not await asyncio.to_thread(self.files.verified, row.serving_key, row.final_sha256)
        ):
            return None
        return self._project(row)

    async def serving_path(self, entry_id: UUID) -> Path | None:
        async with self.factory() as session:
            row = await session.get(StudioGalleryEntry, entry_id)
        if row is None or row.state != "active" or not self._current(row.history):
            return None
        return await asyncio.to_thread(self.files.verified, row.serving_key, row.final_sha256)

    async def reconcile(self) -> None:
        """Finish durable retire/delete work after a crash. Idempotent by design."""
        async with self.factory() as session:
            rows = (
                await session.scalars(
                    select(StudioGalleryEntry).where(StudioGalleryEntry.state == "retired")
                )
            ).all()
        for row in rows:
            await asyncio.to_thread(self.files.remove_owned, row.serving_key)
            async with self.factory() as session, session.begin():
                current = await session.get(StudioGalleryEntry, row.id)
                if current is not None and current.state == "retired":
                    current.cleanup_intent = "complete"
                    outbox = await session.scalar(
                        select(StudioGalleryOutbox).where(
                            StudioGalleryOutbox.entry_id == current.id,
                            StudioGalleryOutbox.kind == "delete",
                        )
                    )
                    if outbox is not None:
                        outbox.state, outbox.completed_at = "complete", _utc_now()

    async def revoke(self, entry_id: UUID, *, reason: str) -> None:
        """Durably retire an active public entry after an operator revocation."""
        if not reason or len(reason) > 160:
            raise PortfolioLibraryServiceError("portfolio revocation reason is invalid")
        async with self.factory() as session, session.begin():
            await self._lock(session)
            row = await session.get(StudioGalleryEntry, entry_id)
            if row is None:
                raise PortfolioLibraryServiceError("portfolio entry is missing")
            if row.state == "retired":
                return
            if row.state != "active":
                raise PortfolioLibraryServiceError("portfolio entry is not active")
            history = dict(row.history or {})
            history["revoked"] = True
            history["revocation_reason"] = reason
            row.history = history
            row.state, row.retired_at, row.cleanup_intent = "retired", _utc_now(), "pending"
            session.add(StudioGalleryOutbox(entry_id=row.id, kind="delete", state="pending"))

    async def _prepare(self, final: PublicFinal) -> StudioGalleryEntry:
        async with self.factory() as session, session.begin():
            row = await session.scalar(
                select(StudioGalleryEntry).where(
                    StudioGalleryEntry.source_instance_key == final.source_instance_key
                )
            )
            if row is not None:
                self._same_final(row, final)
                return row
            row = StudioGalleryEntry(
                source_instance_key=final.source_instance_key,
                source_run_id=final.source_run_id,
                source_revision=final.source_revision,
                source_mode=final.source_mode,
                item_label=final.item_label,
                final_sha256=final.final_sha256,
                accepted_at=final.accepted_at,
                public_suitability_receipt=final.public_suitability_receipt,
                rights_receipt=final.rights_receipt,
                consent_subject_hash=final.consent_subject_hash,
                serving_key=uuid4().hex,
                history=self._sanitize_history(final.history),
                state="copy_pending",
            )
            session.add(row)
            await session.flush()
            return row

    async def _activate(self, entry_id: UUID) -> PortfolioEntry:
        async with self.factory() as session, session.begin():
            await self._lock(session)
            row = await session.get(StudioGalleryEntry, entry_id)
            if row is None:
                raise PortfolioLibraryServiceError("portfolio entry disappeared")
            if row.state == "active":
                return self._project(row)
            if row.state != "copy_pending" or not await asyncio.to_thread(
                self.files.verified, row.serving_key, row.final_sha256
            ):
                raise PortfolioLibraryServiceError("portfolio serving copy is unavailable")
            # Capacity is for current public entries only. Retire stale rows in
            # this same serialized transaction before the new entry joins rank.
            existing = (await session.scalars(self._active_statement())).all()
            for stale in existing:
                if not self._current(stale.history):
                    self._retire(stale, session)
            await session.flush()
            row.state, row.activated_at = "active", _utc_now()
            active = (await session.scalars(self._active_statement())).all()
            for old in active[3:]:
                self._retire(old, session)
            return self._project(row)

    @staticmethod
    def _retire(row: StudioGalleryEntry, session: AsyncSession) -> None:
        if row.state == "retired":
            return
        row.state, row.retired_at, row.cleanup_intent = "retired", _utc_now(), "pending"
        session.add(StudioGalleryOutbox(entry_id=row.id, kind="delete", state="pending"))

    async def _lock(self, session: AsyncSession) -> None:
        lock = await session.get(StudioGalleryLock, 1)
        if lock is None:
            session.add(StudioGalleryLock(id=1, generation=0))
            await session.flush()
        await session.execute(
            update(StudioGalleryLock)
            .where(StudioGalleryLock.id == 1)
            .values(generation=StudioGalleryLock.generation + 1)
        )

    @staticmethod
    def _active_statement():
        return (
            select(StudioGalleryEntry)
            .where(StudioGalleryEntry.state == "active")
            .order_by(
                StudioGalleryEntry.accepted_at.desc(), StudioGalleryEntry.source_instance_key.asc()
            )
        )

    @staticmethod
    def _project(row: StudioGalleryEntry) -> PortfolioEntry:
        return PortfolioEntry(
            row.id,
            row.source_instance_key,
            row.item_label,
            row.source_mode,
            row.final_sha256,
            row.accepted_at,
            row.serving_key,
            dict(row.history or {}),
        )

    @staticmethod
    def _validate_final(final: PublicFinal) -> None:
        if not final.source_instance_key or len(final.source_instance_key) > 160:
            raise PortfolioLibraryServiceError("portfolio source key is invalid")
        if final.source_mode not in {"recorded", "live"}:
            raise PortfolioLibraryServiceError("portfolio source mode is invalid")
        if not final.item_label or len(final.item_label) > 96:
            raise PortfolioLibraryServiceError("portfolio item label is invalid")
        if len(final.final_sha256) != 64 or len(final.consent_subject_hash) != 64:
            raise PortfolioLibraryServiceError("portfolio artifact binding is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", final.final_sha256) or not re.fullmatch(
            r"[0-9a-f]{64}", final.consent_subject_hash
        ):
            raise PortfolioLibraryServiceError("portfolio artifact binding is invalid")
        if not final.public_suitability_receipt:
            raise PortfolioLibraryServiceError("portfolio public suitability receipt is required")
        if not final.rights_receipt:
            raise PortfolioLibraryServiceError("portfolio rights receipt is required")
        if not PortfolioLibraryService._current(final.history):
            raise PortfolioLibraryServiceError("portfolio public evidence is not current")

    @staticmethod
    def validate_source_consent(consent: SourceGalleryConsent) -> None:
        if consent.source_mode not in {"recorded", "live"}:
            raise PortfolioLibraryServiceError("fixture or demo final cannot enter public library")
        if not consent.decision_key or len(consent.decision_key) > 64:
            raise PortfolioLibraryServiceError("portfolio decision key is invalid")
        PortfolioLibraryService._validate_final(
            PublicFinal(
                source_instance_key="validation",
                source_run_id=consent.run_id,
                source_revision=consent.revision,
                source_mode=consent.source_mode,
                item_label=consent.item_label,
                source_final=Path("/not-read"),
                final_sha256=consent.final_hash,
                accepted_at=_utc_now(),
                public_suitability_receipt=consent.public_suitability_receipt,
                rights_receipt=consent.rights_receipt,
                consent_subject_hash=consent.consent_subject_hash,
                history=consent.history,
            )
        )

    @staticmethod
    def _same_final(row: StudioGalleryEntry, final: PublicFinal) -> None:
        if (
            row.final_sha256 != final.final_sha256
            or row.consent_subject_hash != final.consent_subject_hash
            or row.public_suitability_receipt != final.public_suitability_receipt
        ):
            raise PortfolioLibraryServiceError("portfolio source key conflicts with accepted final")

    @staticmethod
    def _sanitize_history(value: dict[str, object]) -> dict[str, object]:
        """Persist a deliberately small public summary, never source receipts."""
        months = value.get("months")
        labels = value.get("labels")
        result: dict[str, object] = {}
        if isinstance(months, int) and 0 <= months <= 12:
            result["months"] = months
        if (
            isinstance(labels, list)
            and len(labels) <= 12
            and all(isinstance(label, str) and len(label) <= 96 for label in labels)
        ):
            result["labels"] = list(labels)
        for key in (
            "rights_profile_expires_at",
            "rights_profile_version",
            "rights_profile_receipt_id",
            "suitability_expires_at",
            "suitability_receipt_id",
            "revoked",
        ):
            if key in value:
                result[key] = value[key]
        return result

    @staticmethod
    def _current(history: dict[str, object] | None) -> bool:
        value = history or {}
        if value.get("revoked") is True:
            return False
        for key in ("rights_profile_expires_at", "suitability_expires_at"):
            raw = value.get(key)
            if not isinstance(raw, str):
                return False
            try:
                expires = datetime.fromisoformat(raw)
            except ValueError:
                return False
            if expires.tzinfo is None or expires <= datetime.now(UTC):
                return False
        return all(
            isinstance(value.get(key), str) and bool(value[key])
            for key in (
                "rights_profile_version",
                "rights_profile_receipt_id",
                "suitability_receipt_id",
            )
        )
