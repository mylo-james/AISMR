from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.storage.models import Base, _utc_now
from myloware.storage.studio_models import StudioGalleryEntry, StudioGalleryProjectionIntent
from myloware.studio.portfolio_library_service import (
    PortfolioLibraryService,
    PublicFinal,
    SourceGalleryConsent,
    record_gallery_projection_intent,
)


async def _service(tmp_path: Path) -> PortfolioLibraryService:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    service = PortfolioLibraryService(
        async_sessionmaker(engine, expire_on_commit=False),
        media_root=tmp_path / "library",
        protected_roots=(tmp_path / "archive", tmp_path / "live"),
    )
    await service.initialize()
    return service


def _final(tmp_path: Path, number: int, *, accepted_at=None) -> PublicFinal:
    source = tmp_path / "live" / f"{number}.mp4"
    source.parent.mkdir(exist_ok=True)
    payload = f"media-{number}".encode()
    source.write_bytes(payload)
    return PublicFinal(
        source_instance_key=f"live:{number}:1",
        source_run_id=uuid4(),
        source_revision=1,
        source_mode="live",
        item_label=f"item {number}",
        source_final=source,
        final_sha256=sha256(payload).hexdigest(),
        accepted_at=accepted_at or (_utc_now() + timedelta(seconds=number)),
        public_suitability_receipt=f"receipt-{number}",
        rights_receipt=f"rights-{number}",
        consent_subject_hash="c" * 64,
        history={
            "months": 12,
            "rights_profile_expires_at": (
                _utc_now().replace(tzinfo=UTC) + timedelta(days=1)
            ).isoformat(),
            "rights_profile_version": "test-v1",
            "rights_profile_receipt_id": f"rights-{number}",
            "suitability_expires_at": (
                _utc_now().replace(tzinfo=UTC) + timedelta(days=1)
            ).isoformat(),
            "suitability_receipt_id": f"suitability-{number}",
        },
    )


@pytest.mark.asyncio
async def test_accept_copies_hashes_then_keeps_three_newest(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    finals = [_final(tmp_path, n) for n in range(4)]
    await asyncio.gather(*(service.accept(final) for final in finals))
    gallery = await service.gallery()
    assert [entry.item_label for entry in gallery] == ["item 3", "item 2", "item 1"]
    async with service.factory() as session:
        active = await session.scalar(
            select(func.count())
            .select_from(StudioGalleryEntry)
            .where(StudioGalleryEntry.state == "active")
        )
    assert active == 3
    assert not (service.files.root / "").is_symlink()


@pytest.mark.asyncio
async def test_accept_is_idempotent_and_source_can_disappear_after_activation(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    final = _final(tmp_path, 1)
    first = await service.accept(final)
    second = await service.accept(final)
    final.source_final.unlink()
    assert first.id == second.id
    assert (await service.entry_for_source(final.source_instance_key)).id == first.id
    assert await service.serving_path(first.id)


@pytest.mark.asyncio
async def test_reconcile_removes_only_retired_owned_serving_copy(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    entries = [await service.accept(_final(tmp_path, n)) for n in range(4)]
    retired = entries[0]
    assert (await service.serving_path(retired.id)) is None
    await service.reconcile()
    assert not service.files.path_for(retired.serving_key).exists()


@pytest.mark.asyncio
async def test_public_projection_drops_untrusted_history_fields(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    original = _final(tmp_path, 1)
    final = replace(original, history={**original.history, "token": "private"})
    entry = await service.accept(final)
    assert entry.history["months"] == 12 and "token" not in entry.history


@pytest.mark.asyncio
async def test_revoke_hides_entry_then_reconcile_removes_owned_copy(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    entry = await service.accept(_final(tmp_path, 8))
    await service.revoke(entry.id, reason="rights revoked")
    assert await service.serving_path(entry.id) is None
    assert await service.gallery() == ()
    await service.reconcile()
    assert not service.files.path_for(entry.serving_key).exists()


@pytest.mark.asyncio
async def test_activation_retires_expired_before_capacity_rank(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    first, second, third = [await service.accept(_final(tmp_path, number)) for number in range(3)]
    async with service.factory() as session, session.begin():
        stale = await session.get(StudioGalleryEntry, first.id)
        assert stale is not None
        history = dict(stale.history)
        history["rights_profile_expires_at"] = "2000-01-01T00:00:00+00:00"
        stale.history = history
    older_but_current = _final(tmp_path, 9, accepted_at=_utc_now() - timedelta(days=1))
    await service.accept(older_but_current)
    async with service.factory() as session:
        rows = (await session.scalars(select(StudioGalleryEntry))).all()
    by_id = {row.id: row.state for row in rows}
    assert by_id[first.id] == "retired"
    assert by_id[second.id] == by_id[third.id] == "active"
    assert sum(state == "active" for state in by_id.values()) == 3


async def _source_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_committed_source_intent_recovers_after_consent_before_copy(tmp_path: Path) -> None:
    source_factory = await _source_factory(tmp_path)
    service = await _service(tmp_path)
    service.source_factory = source_factory
    final = _final(tmp_path, 1)
    consent = SourceGalleryConsent(
        run_id=final.source_run_id,
        revision=final.source_revision,
        final_hash=final.final_sha256,
        decision_key="gallery-opt-in-1",
        consent_subject_hash=final.consent_subject_hash,
        source_mode=final.source_mode,
        item_label=final.item_label,
        public_suitability_receipt=final.public_suitability_receipt,
        rights_receipt=final.rights_receipt,
        history=final.history,
    )
    async with source_factory() as session, session.begin():
        intent = await record_gallery_projection_intent(session, consent=consent)
        intent_id = intent.id
    assert await service.gallery() == ()
    entry = await service.project_source_intent(intent_id, source_final=final.source_final)
    assert entry.final_sha256 == final.final_sha256
    async with source_factory() as session:
        saved = await session.get(StudioGalleryProjectionIntent, intent_id)
    assert saved and saved.state == "complete" and saved.library_entry_id == entry.id


@pytest.mark.asyncio
async def test_retries_after_library_activation_before_source_acknowledgement(
    tmp_path: Path, monkeypatch
) -> None:
    source_factory = await _source_factory(tmp_path)
    service = await _service(tmp_path)
    service.source_factory = source_factory
    final = _final(tmp_path, 2)
    consent = SourceGalleryConsent(
        run_id=final.source_run_id,
        revision=final.source_revision,
        final_hash=final.final_sha256,
        decision_key="gallery-opt-in-2",
        consent_subject_hash=final.consent_subject_hash,
        source_mode=final.source_mode,
        item_label=final.item_label,
        public_suitability_receipt=final.public_suitability_receipt,
        rights_receipt=final.rights_receipt,
        history=final.history,
    )
    async with source_factory() as session, session.begin():
        intent = await record_gallery_projection_intent(session, consent=consent)
        intent_id = intent.id

    async def interrupted_ack(*_args, **_kwargs):
        raise RuntimeError("crash after activation")

    monkeypatch.setattr(service, "_ack_source_intent", interrupted_ack)
    with pytest.raises(RuntimeError, match="crash after activation"):
        await service.project_source_intent(intent_id, source_final=final.source_final)
    assert len(await service.gallery()) == 1
    monkeypatch.undo()
    entry = await service.project_source_intent(intent_id, source_final=final.source_final)
    async with source_factory() as session:
        saved = await session.get(StudioGalleryProjectionIntent, intent_id)
    assert saved and saved.state == "complete" and saved.library_entry_id == entry.id


@pytest.mark.asyncio
async def test_source_outbox_refuses_fixture_even_with_named_receipts(tmp_path: Path) -> None:
    source_factory = await _source_factory(tmp_path)
    final = _final(tmp_path, 3)
    consent = SourceGalleryConsent(
        run_id=final.source_run_id,
        revision=1,
        final_hash=final.final_sha256,
        decision_key="fixture-opt-in",
        consent_subject_hash=final.consent_subject_hash,
        source_mode="fixture",
        item_label=final.item_label,
        public_suitability_receipt="made-up",
        rights_receipt="made-up",
        history={},
    )
    async with source_factory() as session, session.begin():
        with pytest.raises(RuntimeError, match="fixture"):
            await record_gallery_projection_intent(session, consent=consent)


@pytest.mark.asyncio
async def test_source_intent_refuses_changed_final_before_activation(tmp_path: Path) -> None:
    source_factory = await _source_factory(tmp_path)
    service = await _service(tmp_path)
    service.source_factory = source_factory
    final = _final(tmp_path, 4)
    consent = SourceGalleryConsent(
        run_id=final.source_run_id,
        revision=final.source_revision,
        final_hash=final.final_sha256,
        decision_key="gallery-opt-in-4",
        consent_subject_hash=final.consent_subject_hash,
        source_mode=final.source_mode,
        item_label=final.item_label,
        public_suitability_receipt=final.public_suitability_receipt,
        rights_receipt=final.rights_receipt,
        history=final.history,
    )
    async with source_factory() as session, session.begin():
        intent = await record_gallery_projection_intent(session, consent=consent)
        intent_id = intent.id
    final.source_final.write_bytes(b"replaced")
    with pytest.raises(ValueError, match="source final hash mismatch"):
        await service.project_source_intent(intent_id, source_final=final.source_final)
    assert await service.gallery() == ()


def test_rejects_library_root_overlap_and_symlink(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="overlaps"):
        PortfolioLibraryService(
            async_sessionmaker(create_async_engine("sqlite+aiosqlite:///:memory:")),
            media_root=tmp_path / "live",
            protected_roots=(tmp_path / "live",),
        )
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        PortfolioLibraryService(
            async_sessionmaker(create_async_engine("sqlite+aiosqlite:///:memory:")), media_root=link
        )
