from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from botocore.response import StreamingBody
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.config import settings
from myloware.config.studio import StudioSettings
from myloware.storage.models import Base, Job, _utc_now
from myloware.storage.studio_models import StudioAsset, StudioRun
from myloware.storage.studio_store import StudioStore

ROOT = Path(__file__).resolve().parents[2] / "data/recorded/teacup"
BUNDLE_HASH = sha256((ROOT / "fixture-manifest.json").read_bytes()).hexdigest()


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    from myloware.api.routes.studio import get_store, router
    from myloware.storage.database import shutdown_async_db
    from myloware.studio import hosted_queue
    from myloware.workflows.langgraph.graph import get_langgraph_engine

    config = StudioSettings(
        _env_file=None,
        enabled=True,
        mode="recorded",
        recorded_root=ROOT,
        recorded_final_reuse=True,
        recorded_bundle_sha256=BUNDLE_HASH,
        recorded_bucket="aismr-test",
        session_secret="x" * 40,
        media_root=tmp_path / "media",
        fixture_root=tmp_path / "fixture",
        visitor_runs_24h=10,
        ip_runs_24h=20,
        active_runs=10,
    )
    await shutdown_async_db()
    dburl = f"sqlite+aiosqlite:///{tmp_path}/runtime.db"
    monkeypatch.setattr(settings, "database_url", dburl)
    engine = create_async_engine(dburl)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = StudioStore(factory, config)
    monkeypatch.setattr("myloware.studio.service.get_studio_settings", lambda: config)
    monkeypatch.setattr(hosted_queue, "get_studio_settings", lambda: config)
    signals = []

    async def send(*args, **kw):
        signals.append((args, kw))

    monkeypatch.setattr(hosted_queue, "send", send)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as client:
        response = await client.post(
            "/v1/studio/session", json={}, headers={"Origin": config.origin}
        )
        assert response.status_code == 200
        headers = {"Origin": config.origin, "X-CSRF-Token": response.json()["csrf"]}
        yield store, client, headers, signals
    await get_langgraph_engine().shutdown()
    await shutdown_async_db()
    await engine.dispose()


async def start(runtime):
    _store, client, headers, _ = runtime
    response = await client.post(
        "/v1/studio/runs", json={"item": "Teacup", "request_key": "recorded-start"}, headers=headers
    )
    assert response.status_code == 200, response.text
    return UUID(response.json()["id"])


async def decide(runtime, run_id, gate):
    _, client, headers, _ = runtime
    run = (await client.get(f"/v1/studio/runs/{run_id}")).json()
    subject = run["plan_hash"] if gate == "ideas" else run["final"]["review_hash"]
    response = await client.post(
        f"/v1/studio/runs/{run_id}/decisions",
        headers=headers,
        json={
            "gate": gate,
            "decision": "approve",
            "subject_hash": subject,
            "revision": 1,
            "request_key": f"approve-{gate}",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_real_graph_cold_resume_both_reviews_and_scoped_preview(runtime, monkeypatch):
    from myloware.storage import object_store
    from myloware.studio.hosted_queue import process_run
    from myloware.workflows.langgraph.graph import get_langgraph_engine

    class ObjectStore:
        verified = 0

        async def verify_object_async(self, **kwargs):
            self.verified += 1
            assert kwargs["expected_bytes"] == 92455913
            return {"etag": "immutable"}

        async def require_object_etag_async(self, **kwargs):
            assert kwargs["expected_etag"] == "immutable"

        async def presign_get_async(self, **kwargs):
            return "https://example.r2.cloudflarestorage.com/private?signature=sample"

    objects = ObjectStore()
    monkeypatch.setattr(object_store, "get_s3_store", lambda: objects)

    # External generation/render composition must never be reached.
    def forbidden(*args, **kwargs):
        raise AssertionError("provider or renderer constructed")

    monkeypatch.setattr("myloware.studio.service.MonthlyEditor", forbidden)
    monkeypatch.setattr("myloware.studio.service.MonthlyIdeator", forbidden)
    store, client, _headers, signals = runtime
    run_id = await start(runtime)
    assert signals
    assert await process_run(run_id) is False
    assert (await store.get_run(run_id)).status == "idea_review"
    assert await process_run(run_id) is False
    await get_langgraph_engine().shutdown()
    await decide(runtime, run_id, "ideas")
    # Immediate saved-media stages finish under one bounded SQL claim. The
    # subscriber must stop at final review without another platform delivery.
    assert await process_run(run_id) is False
    run = await store.get_run(run_id)
    assert run.status == "final_review"
    assert run.final_metadata["rendered_now"] is False
    assert run.final_metadata["moderated_now"] is False
    assert objects.verified == 1
    assert await process_run(run_id) is False
    assert objects.verified == 1
    async with store.factory() as session:
        assets = (
            await session.scalars(select(StudioAsset).where(StudioAsset.run_id == run_id))
        ).all()
    assert len(assets) == 24
    preview = await client.get(f"/v1/studio/runs/{run_id}/preview")
    assert preview.status_code == 307, preview.text
    assert preview.headers["cache-control"] == "private, no-store"
    saved = client.cookies
    client.cookies = httpx.Cookies()
    unauthorized = await client.get(f"/v1/studio/runs/{run_id}/preview")
    assert unauthorized.status_code == 404
    client.cookies = saved
    assert (await decide(runtime, run_id, "final"))["status"] == "video_complete"
    await process_run(run_id)
    await process_run(run_id)
    async with store.factory() as session:
        jobs = (await session.scalars(select(Job).where(Job.run_id == run_id))).all()
    assert all(job.status == "succeeded" for job in jobs)
    assert (await client.get("/v1/studio/gallery")).json() == {"items": []}


@pytest.mark.asyncio
async def test_expired_hosted_reviews_release_capacity_without_erasing_history(runtime):
    from myloware.studio.hosted_queue import process_run

    store, client, headers, _ = runtime
    store.config.active_runs = 1
    run_id = await start(runtime)
    await process_run(run_id)
    assert (await store.get_run(run_id)).status == "idea_review"

    async def another():
        return await client.post(
            "/v1/studio/runs",
            json={"item": "Teacup", "request_key": "recorded-second"},
            headers=headers,
        )

    assert (await another()).status_code == 429
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        run.expires_at = _utc_now() - timedelta(seconds=1)

    # Ordinary recorded rendering retains its existing admission policy.
    store.config.recorded_final_reuse = False
    assert (await another()).status_code == 429
    store.config.recorded_final_reuse = True
    assert (await another()).status_code == 200
    expired = await store.get_run(run_id)
    assert expired.status == "idea_review"
    assert expired.plan_hash
    decision = await client.post(
        f"/v1/studio/runs/{run_id}/decisions",
        headers=headers,
        json={
            "gate": "ideas",
            "decision": "approve",
            "subject_hash": expired.plan_hash,
            "revision": 1,
            "request_key": "expired-approval",
        },
    )
    assert decision.status_code == 401
    assert decision.json()["error"] == "session_expired"


@pytest.mark.asyncio
async def test_stream_verification_rejects_wrong_bytes_and_closes_body():
    from myloware.storage.object_store import S3Store

    raw = b"verified bytes"

    class S3:
        def get_object(self, **kwargs):
            self.body = StreamingBody(BytesIO(raw), len(raw))
            return {"Body": self.body, "ContentLength": len(raw), "ETag": "etag"}

    s3 = S3()
    store = S3Store.__new__(S3Store)
    store._client = s3
    assert await store.verify_object_async(
        uri="s3://bucket/key", expected_sha256=sha256(raw).hexdigest(), expected_bytes=len(raw)
    ) == {"etag": "etag"}
    assert s3.body._raw_stream.closed
    with pytest.raises(ValueError):
        await store.verify_object_async(
            uri="s3://bucket/key", expected_sha256="0" * 64, expected_bytes=len(raw)
        )
    assert s3.body._raw_stream.closed


def test_hosted_configuration_rejects_live_effects():
    for kw in [
        {"render_real": True},
        {"fal_key": "secret"},
        {"live_enabled": True},
        {"mode": "live"},
    ]:
        with pytest.raises(ValueError):
            StudioSettings(
                _env_file=None,
                recorded_root=ROOT,
                recorded_final_reuse=True,
                recorded_bundle_sha256=BUNDLE_HASH,
                recorded_bucket="test",
                session_secret="x" * 40,
                **({"mode": "recorded"} | kw),
            )


@pytest.mark.asyncio
async def test_mutated_plan_cannot_reuse_the_saved_final(runtime):
    from myloware.studio.hosted_queue import process_run

    store, _client, _headers, _signals = runtime
    run_id = await start(runtime)
    await process_run(run_id)
    await decide(runtime, run_id, "ideas")
    # Corrupt the approved plan before the bounded batch selects any saved media.
    async with store.factory.begin() as session:
        run = await session.get(StudioRun, run_id)
        run.plan_hash = run.plan_approved_hash = "b" * 64
    assert await process_run(run_id) is True
    run = await store.get_run(run_id)
    assert run.status in {"failed", "blocked"}
    assert run.final_artifact_id is None


@pytest.mark.asyncio
async def test_gallery_direct_route_closed_and_changed_storage_is_unavailable(runtime, monkeypatch):
    from myloware.storage.studio_store import StudioError
    from myloware.studio.hosted_recorded import bundle_entry, recorded_preview_url

    store, client, _headers, _signals = runtime
    run_id = await start(runtime)
    assert (await client.get(f"/v1/studio/gallery/{run_id}/video")).status_code == 404

    class ChangedStorage:
        async def require_object_etag_async(self, **kwargs):
            raise ValueError("changed")

    monkeypatch.setattr("myloware.storage.object_store.get_s3_store", lambda: ChangedStorage())
    with pytest.raises(StudioError) as error:
        await recorded_preview_url(
            store.config, bundle_entry(store.config, "approved_local_render")["sha256"], "old"
        )
    assert error.value.status == 404


@pytest.mark.asyncio
async def test_bounded_claim_never_takes_another_runs_job(runtime):
    from uuid import uuid4

    from myloware.storage.repositories import JobRepository

    store, _client, _headers, _signals = runtime
    run_id = await start(runtime)
    async with store.factory() as session:
        repo = JobRepository(session)
        assert (
            await repo.claim_next_async(
                worker_id="unrelated", run_id=uuid4(), job_types=("studio.advance",)
            )
            is None
        )
        job = await repo.claim_next_async(
            worker_id="correct", run_id=run_id, job_types=("studio.advance",)
        )
        assert job is not None and job.run_id == run_id
        await session.commit()
    async with store.factory() as session:
        repo = JobRepository(session)
        assert (
            await repo.claim_next_async(
                worker_id="duplicate", run_id=run_id, job_types=("studio.advance",)
            )
            is None
        )
