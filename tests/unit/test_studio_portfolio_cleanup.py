from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import myloware.studio.library_service as library_service_module
from myloware.config.studio import StudioSettings
from myloware.storage.models import Base, Run, _utc_now
from myloware.storage.studio_models import (
    PortfolioLibraryBase,
    StudioCostLedger,
    StudioDecision,
    StudioGalleryEntry,
    StudioGalleryProjectionIntent,
    StudioRun,
)
from myloware.storage.studio_store import StudioStore
from myloware.studio.library_service import StudioLibraryService
from myloware.workers.handlers import handle_job


async def _store(tmp_path: Path) -> tuple[StudioStore, object]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return (
        StudioStore(
            async_sessionmaker(engine, expire_on_commit=False),
            StudioSettings(
                enabled=True,
                mode="fixture",
                ip_runs_24h=20,
                media_root=tmp_path / "media",
                fixture_root=tmp_path / "fixtures",
            ),
        ),
        engine,
    )


async def _run(
    store: StudioStore,
    *,
    mode: str,
    status: str,
    approved: bool,
    name: str,
) -> StudioRun:
    run_id = uuid4()
    payload = name.encode()
    final = store.config.media_root / str(run_id) / "final.mp4"
    final.parent.mkdir(parents=True)
    final.write_bytes(payload)
    async with store.transaction() as session:
        session.add(Run(id=run_id, workflow_name="monthly", input=name, status="pending"))
        await session.flush()
        run = StudioRun(
            run_id=run_id,
            visitor_id=f"visitor-{run_id.hex}",
            start_key=f"start-{run_id.hex}",
            read_token_hash=run_id.hex * 2,
            item_id="teacup",
            item_text=name,
            mode=mode,
            status=status,
            final_hash=sha256(payload).hexdigest(),
            final_metadata={"media_verified": True, "path": str(final.resolve())},
            expires_at=_utc_now() + timedelta(days=1),
        )
        session.add(run)
        if approved:
            session.add(
                StudioDecision(
                    run_id=run_id,
                    visitor_id=run.visitor_id,
                    gate="final",
                    revision=1,
                    subject_hash="a" * 64,
                    request_key=f"decision-{run_id.hex}",
                    decision="approve",
                    payload={},
                    expires_at=_utc_now() + timedelta(days=1),
                )
            )
    return run


@pytest.mark.asyncio
async def test_unknown_cancelled_media_survives_unrelated_legacy_cleanup(tmp_path: Path) -> None:
    store, engine = await _store(tmp_path)
    try:
        protected = await _run(
            store, mode="live", status="cancelled", approved=False, name="unknown-final"
        )
        trigger = await _run(
            store, mode="fixture", status="video_complete", approved=True, name="trigger-final"
        )
        async with store.transaction() as session:
            session.add(
                StudioCostLedger(
                    run_id=protected.run_id,
                    profile_version="owner-v1",
                    stage="render",
                    operation_key="initial",
                    reserved_usd=1,
                    cost_state="unknown",
                )
            )
        await StudioLibraryService(store).cleanup(trigger.run_id)
        assert (store.config.media_root / str(protected.run_id) / "final.mp4").is_file()
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_private_live_final_is_not_selected_by_legacy_three_item_cap(tmp_path: Path) -> None:
    store, engine = await _store(tmp_path)
    try:
        private = await _run(
            store, mode="live", status="video_complete", approved=True, name="private-live"
        )
        trigger = await _run(
            store, mode="fixture", status="video_complete", approved=True, name="trigger-final"
        )
        service = StudioLibraryService(store)
        async with store.factory() as session:
            _, candidates = await service._candidates(session)
        private_candidate = next(
            candidate for candidate in candidates if candidate.run_id == private.run_id
        )
        assert private_candidate.completed_at is None
        await service.cleanup(trigger.run_id)
        assert (store.config.media_root / str(private.run_id) / "final.mp4").is_file()
        saved = await store.get_run(private.run_id)
        assert "retention" not in (saved.final_metadata or {})
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_complete_projection_ack_recovers_owned_source_cleanup_with_claim_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source_engine = await _store(tmp_path)
    library_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    try:
        projected = await _run(
            store, mode="live", status="video_complete", approved=True, name="projected-final"
        )
        source_final = store.config.media_root / str(projected.run_id) / "final.mp4"
        intent_id = uuid4()
        entry_id = uuid4()
        source_key = f"live:{projected.run_id}:1:{projected.final_hash}"
        async with store.transaction() as session:
            session.add(
                StudioGalleryProjectionIntent(
                    id=intent_id,
                    run_id=projected.run_id,
                    revision=1,
                    final_hash=projected.final_hash,
                    decision_key="gallery-consent",
                    consent_subject_hash="c" * 64,
                    source_instance_key=source_key,
                    source_mode="live",
                    item_label="projected-final",
                    public_suitability_receipt="suitability-receipt",
                    rights_receipt="rights-receipt",
                    history={},
                    state="complete",
                    library_entry_id=entry_id,
                    completed_at=_utc_now(),
                )
            )
        async with library_engine.begin() as connection:
            await connection.run_sync(PortfolioLibraryBase.metadata.create_all)
        library_factory = async_sessionmaker(library_engine, expire_on_commit=False)
        async with library_factory() as session, session.begin():
            session.add(
                StudioGalleryEntry(
                    id=entry_id,
                    source_instance_key=source_key,
                    source_run_id=projected.run_id,
                    source_revision=1,
                    source_mode="live",
                    item_label="projected-final",
                    final_sha256=projected.final_hash,
                    accepted_at=_utc_now(),
                    public_suitability_receipt="suitability-receipt",
                    rights_receipt="rights-receipt",
                    consent_subject_hash="c" * 64,
                    serving_key="serving-final",
                    history={},
                    state="active",
                )
            )

        class FakePortfolio:
            async def initialize(self) -> None:
                return None

            async def reconcile(self) -> None:
                return None

        @asynccontextmanager
        async def open_fake_portfolio(_store: StudioStore):
            yield FakePortfolio()

        checks = 0

        async def current_claim(_session) -> None:  # type: ignore[no-untyped-def]
            nonlocal checks
            checks += 1

        original_remove = library_service_module.StudioLibrary.remove_projected_source

        def fenced_remove(self, run_id: UUID):  # type: ignore[no-untyped-def]
            assert checks > 0
            return original_remove(self, run_id)

        renderer_calls: list[tuple[UUID, str | None]] = []

        async def renderer_cleanup(_self, run: StudioRun) -> None:  # type: ignore[no-untyped-def]
            renderer_calls.append((run.run_id, run.final_hash))

        monkeypatch.setattr(
            "myloware.storage.database.get_async_session_factory", lambda: store.factory
        )
        monkeypatch.setattr("myloware.config.studio.get_studio_settings", lambda: store.config)
        monkeypatch.setattr(
            "myloware.studio.portfolio_runtime.open_portfolio_library", open_fake_portfolio
        )
        monkeypatch.setattr("myloware.workers.claims.require_current_claim", current_claim)
        monkeypatch.setattr(library_service_module, "require_current_claim", current_claim)
        monkeypatch.setattr(
            library_service_module.StudioLibrary, "remove_projected_source", fenced_remove
        )
        monkeypatch.setattr(StudioLibraryService, "_renderer_copy", renderer_cleanup)

        async def rollback() -> None:
            return None

        await handle_job(
            job_type="studio.cleanup",
            run_id=projected.run_id,
            payload={},
            session_run_repo=SimpleNamespace(),
            session_artifact_repo=SimpleNamespace(),
            session_job_repo=SimpleNamespace(session=SimpleNamespace(rollback=rollback)),
            llama_client=SimpleNamespace(),
        )

        assert not source_final.exists()
        assert checks > 0
        assert renderer_calls == [(projected.run_id, projected.final_hash)]
    finally:
        await source_engine.dispose()  # type: ignore[union-attr]
        await library_engine.dispose()
