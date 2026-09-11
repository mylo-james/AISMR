from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from myloware.config.studio import StudioSettings
from myloware.storage.models import Base, _utc_now
from myloware.storage.repositories import StaleJobClaim
from myloware.storage.studio_models import StudioDecision, StudioRun
from myloware.storage.studio_store import StudioStore
from myloware.studio.library_service import StudioLibraryService


async def _store(tmp_path: Path) -> tuple[StudioStore, object]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}")
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


async def _complete(store: StudioStore, offset: int) -> StudioRun:
    visitor, _ = await store.new_visitor(f"127.0.0.{offset + 1}")
    run_id = await store.admit(
        visitor_id=visitor.id,
        item_text="teacup",
        start_key=f"retention-{offset:08d}",
        ip="127.0.0.1",
    )
    payload = f"video-{offset}".encode()
    final = store.config.media_root / str(run_id) / "final.mp4"
    final.parent.mkdir(parents=True)
    final.write_bytes(payload)
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        assert run is not None
        run.status = "video_complete"
        run.final_hash = sha256(payload).hexdigest()
        run.final_metadata = {"media_verified": True}
        session.add(
            StudioDecision(
                run_id=run_id,
                visitor_id=visitor.id,
                gate="final",
                revision=1,
                subject_hash="a" * 64,
                request_key=f"approve-{offset:08d}",
                decision="approve",
                payload={},
                expires_at=_utc_now() + timedelta(hours=1),
            )
        )
        return run


@pytest.mark.asyncio
async def test_commit_failure_never_starts_filesystem_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, engine = await _store(tmp_path)
    try:
        run = await _complete(store, 1)
        service = StudioLibraryService(store)
        applied = False

        def apply_never(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal applied
            applied = True
            raise AssertionError("filesystem mutation must follow an intent commit")

        monkeypatch.setattr(service.files, "apply", apply_never)

        def fail_commit(_session: Session) -> None:
            raise RuntimeError("intent commit failed")

        event.listen(Session, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="intent commit failed"):
            await service.cleanup(run.run_id)
        assert not applied
        assert (store.config.media_root / str(run.run_id) / "final.mp4").is_file()
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stale_refence_leaves_committed_intent_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, engine = await _store(tmp_path)
    try:
        run = await _complete(store, 1)
        service = StudioLibraryService(store)
        applied = False

        async def stale_claim(_session) -> None:  # type: ignore[no-untyped-def]
            raise StaleJobClaim("cleanup claim was reclaimed")

        def apply_never(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal applied
            applied = True

        monkeypatch.setattr("myloware.studio.library_service.require_current_claim", stale_claim)
        monkeypatch.setattr(service.files, "apply", apply_never)
        with pytest.raises(StaleJobClaim, match="reclaimed"):
            await service.cleanup(run.run_id)
        assert not applied
        assert (store.config.media_root / str(run.run_id) / "final.mp4").is_file()
        async with store.factory() as session:
            saved = await session.get(StudioRun, run.run_id)
            assert saved is not None
            assert (saved.final_metadata or {})["retention_cleanup"] == {
                "state": "pending",
                "action": "keep",
            }
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_retry_finishes_pending_eviction_after_interrupted_filesystem_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, engine = await _store(tmp_path)
    try:
        runs = [await _complete(store, offset) for offset in range(4)]
        service = StudioLibraryService(store)
        original_apply = service.files.apply
        calls = 0

        def apply_then_interrupt(plan):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            receipt = original_apply(plan)
            if calls == 1:
                raise RuntimeError("interrupted after bytes moved")
            return receipt

        monkeypatch.setattr(service.files, "apply", apply_then_interrupt)
        with pytest.raises(RuntimeError, match="interrupted"):
            await service.cleanup(runs[-1].run_id)
        evicted = next(
            run
            for run in runs
            if not (store.config.media_root / str(run.run_id) / "final.mp4").exists()
        )
        async with store.factory() as session:
            pending = await session.get(StudioRun, evicted.run_id)
            assert pending is not None
            assert (pending.final_metadata or {})["retention_cleanup"]["action"] == "evict"

        await service.cleanup(runs[-1].run_id)
        async with store.factory() as session:
            saved = await session.get(StudioRun, evicted.run_id)
            assert saved is not None
            assert (saved.final_metadata or {}).get("retention") == "evicted"
            assert "retention_cleanup" not in (saved.final_metadata or {})
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_new_completion_reselects_interrupted_pending_keeps_to_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, engine = await _store(tmp_path)
    try:
        runs = [await _complete(store, offset) for offset in range(3)]
        service = StudioLibraryService(store)

        async def stale_claim(_session) -> None:  # type: ignore[no-untyped-def]
            raise StaleJobClaim("cleanup claim was reclaimed")

        with monkeypatch.context() as patch:
            patch.setattr("myloware.studio.library_service.require_current_claim", stale_claim)
            with pytest.raises(StaleJobClaim, match="reclaimed"):
                await service.cleanup(runs[-1].run_id)

        newest = await _complete(store, 3)
        await service.cleanup(newest.run_id)
        assert len(list(store.config.media_root.glob("*/final.mp4"))) == 3
        async with store.factory() as session:
            saved = [await session.get(StudioRun, run.run_id) for run in [*runs, newest]]
        assert (
            sum(
                (run.final_metadata or {}).get("retention") == "evicted"
                for run in saved
                if run is not None
            )
            == 1
        )
    finally:
        await engine.dispose()  # type: ignore[union-attr]
