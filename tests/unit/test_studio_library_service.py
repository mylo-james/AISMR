from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import myloware.studio.library_service as library_service_module
from myloware.config.studio import StudioSettings
from myloware.storage.models import Base, _utc_now
from myloware.storage.studio_models import StudioDecision, StudioRun
from myloware.storage.studio_store import StudioStore
from myloware.studio.library_service import StudioLibraryService


async def _store(tmp_path: Path) -> StudioStore:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return StudioStore(
        async_sessionmaker(engine, expire_on_commit=False),
        StudioSettings(
            enabled=True,
            mode="recorded",
            recorded_root=tmp_path / "archive",
            ip_runs_24h=20,
            media_root=tmp_path / "media",
            fixture_root=tmp_path / "fixtures",
        ),
    )


async def _complete(
    store: StudioStore,
    *,
    offset: int,
    mode: str = "recorded",
    approved: bool = True,
    corrupt: bool = False,
) -> StudioRun:
    visitor, _ = await store.new_visitor(f"127.0.0.{offset + 1}")
    run_id = await store.admit(
        visitor_id=visitor.id, item_text="teacup", start_key=f"key-{offset:08d}", ip="127.0.0.1"
    )
    payload = f"video-{offset}".encode()
    final = store.config.media_root / str(run_id) / "final.mp4"
    final.parent.mkdir(parents=True)
    final.write_bytes(payload)
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        assert run
        run.mode, run.status, run.final_hash = mode, "video_complete", sha256(payload).hexdigest()
        run.final_metadata = {"media_verified": True, "duration_seconds": 1}
        if corrupt:
            final.write_bytes(b"corrupt")
        if approved:
            session.add(
                StudioDecision(
                    run_id=run_id,
                    visitor_id=visitor.id,
                    gate="final",
                    revision=1,
                    subject_hash="a" * 64,
                    request_key=f"final-{offset:08d}",
                    decision="approve",
                    payload={},
                    expires_at=_utc_now() + timedelta(hours=1),
                )
            )
        return run


@pytest.mark.asyncio
async def test_gallery_is_empty_before_acceptance_and_limits_to_three_newest(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    runs = [await _complete(store, offset=index) for index in range(4)]
    service = StudioLibraryService(store)
    items = await service.gallery()
    assert len(items) == 3
    assert {item["id"] for item in items}.issubset({str(run.run_id) for run in runs})
    unapproved = await _complete(store, offset=9, approved=False)
    corrupt = await _complete(store, offset=10, corrupt=True)
    items = await service.gallery()
    assert str(unapproved.run_id) not in {item["id"] for item in items}
    assert str(corrupt.run_id) not in {item["id"] for item in items}


@pytest.mark.asyncio
async def test_cleanup_evicts_fourth_final_and_records_sql_retention(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    runs = [await _complete(store, offset=index) for index in range(4)]
    service = StudioLibraryService(store)
    await service.cleanup(runs[-1].run_id)
    remaining = list(store.config.media_root.glob("*/final.mp4"))
    assert len(remaining) == 3
    async with store.factory() as session:
        saved = [await session.get(StudioRun, run.run_id) for run in runs]
    assert (
        sum(bool((run.final_metadata or {}).get("retention") == "evicted") for run in saved if run)
        == 1
    )


@pytest.mark.asyncio
async def test_gallery_empty_before_any_final_approval(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await _complete(store, offset=1, approved=False)
    assert await StudioLibraryService(store).gallery() == []


@pytest.mark.asyncio
async def test_renderer_cleanup_sends_bound_delete_and_marks_copy_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await _store(tmp_path)
    run = await _complete(store, offset=1)
    async with store.transaction() as session:
        current = await session.get(StudioRun, run.run_id)
        assert current
        current.render_job_id, current.render_input_hash = "job/one", "b" * 64
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(library_service_module.settings, "remotion_api_secret", "secret")
    monkeypatch.setattr(
        library_service_module.settings, "remotion_service_url", "http://renderer.test"
    )
    monkeypatch.setattr(
        library_service_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda request: (
                    seen.append(request),
                    httpx.Response(200, json={"status": "deleted"}),
                )[1]
            ),
            **kwargs,
        ),
    )
    await StudioLibraryService(store).cleanup(run.run_id)
    assert len(seen) == 1 and seen[0].method == "DELETE"
    assert str(seen[0].url).endswith("/api/render/job%2Fone/output")
    assert __import__("json").loads(seen[0].content) == {
        "run_id": str(run.run_id),
        "input_hash": "b" * 64,
        "expected_sha256": run.final_hash,
    }
    async with store.factory() as session:
        saved = await session.get(StudioRun, run.run_id)
        assert (
            saved and saved.final_metadata and saved.final_metadata["renderer_copy_removed"] is True
        )


@pytest.mark.asyncio
async def test_renderer_cleanup_conflict_keeps_completed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await _store(tmp_path)
    run = await _complete(store, offset=2)
    async with store.transaction() as session:
        current = await session.get(StudioRun, run.run_id)
        assert current
        current.render_job_id, current.render_input_hash = "job", "c" * 64
    real_client = httpx.AsyncClient
    monkeypatch.setattr(library_service_module.settings, "remotion_api_secret", "secret")
    monkeypatch.setattr(
        library_service_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(409)), **kwargs
        ),
    )
    with pytest.raises(Exception, match="renderer_cleanup_failed"):
        await StudioLibraryService(store).cleanup(run.run_id)
    async with store.factory() as session:
        saved = await session.get(StudioRun, run.run_id)
        assert saved and saved.status == "video_complete"


@pytest.mark.asyncio
async def test_final_acceptance_queues_cleanup_once(tmp_path: Path) -> None:
    from sqlalchemy import select

    from myloware.storage.models import Job

    store = await _store(tmp_path)
    run = await _complete(store, offset=1, approved=False)
    async with store.transaction() as session:
        saved = await session.get(StudioRun, run.run_id)
        saved.status = "final_review"
    run = await store.get_run(run.run_id)
    decision = {
        "run_id": run.run_id,
        "visitor_id": run.visitor_id,
        "gate": "final",
        "revision": 1,
        "subject_hash": store.final_review_hash(run),
        "decision": "approve",
        "decision_key": "accept-final-1",
    }
    await store.decide(**decision)
    await store.decide(**decision)
    async with store.factory() as session:
        jobs = (await session.scalars(select(Job).where(Job.job_type == "studio.cleanup"))).all()
    assert len(jobs) == 1 and jobs[0].run_id == run.run_id
    assert (await store.get_run(run.run_id)).status == "video_complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["symlink", "bytes"])
async def test_gallery_revalidates_replacement_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    from fastapi import FastAPI

    from myloware.api.routes.studio import get_store, router

    store = await _store(tmp_path)
    run = await _complete(store, offset=1)
    original = StudioLibraryService.gallery
    final = store.config.media_root / str(run.run_id) / "final.mp4"
    external = tmp_path / "private.mp4"
    external.write_bytes(b"private content")

    async def swap_after_selection(service):
        items = await original(service)
        final.unlink()
        if replacement == "symlink":
            final.symlink_to(external)
        else:
            final.write_bytes(b"changed content")
        return items

    monkeypatch.setattr(StudioLibraryService, "gallery", swap_after_selection)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=store.config.origin
    ) as client:
        result = await client.get(f"/v1/studio/gallery/{run.run_id}/video")
    assert result.status_code == 404 and result.json()["error"] == "gallery_video_unavailable"
    assert external.read_bytes() == b"private content"


@pytest.mark.asyncio
async def test_gallery_byte_ranges_and_review_privacy(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from myloware.api.routes.studio import get_store, router

    store = await _store(tmp_path)
    run = await _complete(store, offset=1)
    review = await _complete(store, offset=2, approved=False)
    async with store.transaction() as session:
        pending = await session.get(StudioRun, review.run_id)
        pending.status = "final_review"
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=store.config.origin
    ) as client:
        assert (await client.get(f"/v1/studio/gallery/{review.run_id}/video")).status_code == 404
        response = await client.get(
            f"/v1/studio/gallery/{run.run_id}/video", headers={"Range": "bytes=0-3"}
        )
    # Legacy recorded playback is allowed on loopback only. Private review
    # above remains unavailable through the unauthenticated gallery route.
    assert response.status_code == 206
    assert response.content == b"vide"


@pytest.mark.asyncio
async def test_cleanup_worker_trims_files_without_provider_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from myloware.storage.repositories import ArtifactRepository, JobRepository, RunRepository
    from myloware.workers.handlers import handle_job

    store = await _store(tmp_path)
    runs = [await _complete(store, offset=index) for index in range(4)]
    monkeypatch.setattr(
        "myloware.storage.database.get_async_session_factory", lambda: store.factory
    )
    monkeypatch.setattr("myloware.config.studio.get_studio_settings", lambda: store.config)

    def forbidden_composition():
        raise AssertionError("cleanup must not construct provider clients")

    monkeypatch.setattr("myloware.studio.service.build_studio_service", forbidden_composition)
    async with store.factory() as session:
        await session.execute(select(StudioRun))
        assert session.in_transaction()
        await handle_job(
            job_type="studio.cleanup",
            run_id=runs[-1].run_id,
            payload={},
            session_run_repo=RunRepository(session),
            session_artifact_repo=ArtifactRepository(session),
            session_job_repo=JobRepository(session),
            llama_client=object(),
        )
        assert not session.in_transaction()
    assert len(list(store.config.media_root.glob("*/final.mp4"))) == 3
    saved = await store.get_run(runs[0].run_id)
    assert saved.final_metadata["retention"] == "evicted" and saved.status == "video_complete"
