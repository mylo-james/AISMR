"""ASGI boundary tests for AISMR visitor and renderer authority.

These tests use real file-backed SQLite state and the production studio routers.
They do not construct provider clients or contact external services.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from myloware.api.routes.studio import get_store
from myloware.api.routes.studio import router as studio_router
from myloware.api.routes.studio_internal import router as internal_router
from myloware.config import settings
from myloware.config.studio import StudioSettings
from myloware.storage.models import Base
from myloware.storage.studio_models import StudioDecision, StudioRun
from myloware.storage.studio_store import StudioStore
from myloware.workflows.scenes import deterministic_scene_plan

ORIGIN = "http://127.0.0.1:8311"


@dataclass
class StudioHttp:
    app: FastAPI
    store: StudioStore
    engine: AsyncEngine


@pytest.fixture
async def studio_http(tmp_path: Path) -> StudioHttp:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'studio-http.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = StudioSettings(
        enabled=True,
        origin=ORIGIN,
        active_runs=3,
        visitor_runs_24h=1,
        ip_runs_24h=1,
        planner_version="creative-v2",
        media_root=tmp_path / "media",
        fixture_root=tmp_path / "fixtures",
    )
    store = StudioStore(async_sessionmaker(engine, expire_on_commit=False), config)
    app = FastAPI()
    app.include_router(studio_router)
    app.include_router(internal_router)
    app.dependency_overrides[get_store] = lambda: store
    try:
        yield StudioHttp(app=app, store=store, engine=engine)
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4311)),
        base_url=ORIGIN,
    )


async def _session(client: httpx.AsyncClient, cookie_name: str) -> dict[str, str]:
    response = await client.post("/v1/studio/session", headers={"Origin": ORIGIN}, json={})
    assert response.status_code == 200
    body = response.json()
    assert client.cookies.get(cookie_name) is not None
    return {"Origin": ORIGIN, "X-CSRF-Token": body["csrf"]}


@pytest.mark.asyncio
async def test_session_csrf_origin_and_fixed_quota_ignore_spoofed_headers(
    studio_http: StudioHttp,
) -> None:
    async with _client(studio_http.app) as owner:
        headers = await _session(owner, studio_http.store.config.session_cookie_name)
        missing = await owner.post(
            "/v1/studio/runs",
            headers={"Origin": ORIGIN},
            json={"item": "teacup", "request_key": "csrf-missing"},
        )
        assert missing.status_code == 403 and missing.json() == {"error": "csrf_required"}
        wrong = await owner.post(
            "/v1/studio/runs",
            headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
            json={"item": "teacup", "request_key": "csrf-wrong"},
        )
        assert wrong.status_code == 403 and wrong.json() == {"error": "csrf_required"}
        admitted = await owner.post(
            "/v1/studio/runs",
            headers=headers,
            json={"item": "teacup", "request_key": "csrf-valid"},
        )
        assert admitted.status_code == 200
        run_id = admitted.json()["id"]
        reload = await owner.get("/v1/studio/session")
        assert reload.status_code == 200 and reload.json()["latest_run_id"] == run_id

    async with _client(studio_http.app) as spoofed:
        spoofed_headers = await _session(spoofed, studio_http.store.config.session_cookie_name)
        spoofed_headers.update({"Authorization": "Bearer owner", "X-Forwarded-For": "198.51.100.9"})
        rejected = await spoofed.post(
            "/v1/studio/runs",
            headers=spoofed_headers,
            json={"item": "cake", "request_key": "spoofed-quota"},
        )
        assert rejected.status_code == 429
        assert rejected.json() == {"error": "network_allowance_exhausted"}


@pytest.mark.asyncio
async def test_decisions_are_owner_and_revision_bound_and_share_is_view_only(
    studio_http: StudioHttp,
) -> None:
    async with _client(studio_http.app) as owner, _client(studio_http.app) as other:
        owner_headers = await _session(owner, studio_http.store.config.session_cookie_name)
        other_headers = await _session(other, studio_http.store.config.session_cookie_name)
        created = await owner.post(
            "/v1/studio/runs",
            headers=owner_headers,
            json={"item": "teacup", "request_key": "owner-run"},
        )
        run_id = UUID(created.json()["id"])
        async with studio_http.store.factory.begin() as session:
            run = await session.get(StudioRun, run_id)
            assert run is not None
            run.status, run.plan_hash = "idea_review", "a" * 64

        private = await other.get(f"/v1/studio/runs/{run_id}")
        assert private.status_code == 404 and private.json() == {"error": "run_not_found"}
        denied = await other.post(
            f"/v1/studio/runs/{run_id}/decisions",
            headers=other_headers,
            json={
                "gate": "ideas",
                "decision": "approve",
                "revision": 1,
                "subject_hash": "a" * 64,
                "request_key": "other-denied",
            },
        )
        assert denied.status_code == 404 and denied.json() == {"error": "run_not_found"}

        share = studio_http.store.share_token(run_id)
        shared = await other.get(f"/v1/studio/runs/{run_id}?share={share}")
        assert shared.status_code == 200 and shared.json()["can_act"] is False
        stale = await owner.post(
            f"/v1/studio/runs/{run_id}/decisions",
            headers=owner_headers,
            json={
                "gate": "ideas",
                "decision": "approve",
                "revision": 2,
                "subject_hash": "a" * 64,
                "request_key": "stale-revision",
            },
        )
        assert stale.status_code == 409 and stale.json() == {"error": "stale_review"}
        wrong_hash = await owner.post(
            f"/v1/studio/runs/{run_id}/decisions",
            headers=owner_headers,
            json={
                "gate": "ideas",
                "decision": "approve",
                "revision": 1,
                "subject_hash": "b" * 64,
                "request_key": "stale-hash",
            },
        )
        assert wrong_hash.status_code == 409 and wrong_hash.json() == {"error": "stale_review"}


@pytest.mark.asyncio
async def test_creative_v2_targeted_revision_is_exact_plan_idempotent_and_validated(
    studio_http: StudioHttp,
) -> None:
    async with _client(studio_http.app) as owner:
        headers = await _session(owner, studio_http.store.config.session_cookie_name)
        created = await owner.post(
            "/v1/studio/runs",
            headers=headers,
            json={"item": "teacup", "request_key": "creative-v2-run"},
        )
        assert created.status_code == 200
        run_id = UUID(created.json()["id"])
        plan = deterministic_scene_plan(run_id, "teacup")
        await studio_http.store.save_plan(plan, moderation={"accepted": True})
        current = await owner.get(f"/v1/studio/runs/{run_id}")
        assert current.status_code == 200
        current_plan = current.json()["plan"]
        assert current_plan["schema_version"] == 2
        assert "scenes" in current_plan and "ideas" not in current_plan
        assert all(
            set(scene) == {"ordinal", "title", "visual_prompt"} for scene in current_plan["scenes"]
        )
        base_payload = {
            "gate": "ideas",
            "decision": "revise",
            "revision": 1,
            "subject_hash": plan.canonical_sha256,
            "request_key": "creative-v2-revise",
        }
        malformed = await owner.post(
            f"/v1/studio/runs/{run_id}/decisions",
            headers=headers,
            json={
                **base_payload,
                "revision_feedback": {"replace_ordinals": [13]},
            },
        )
        assert malformed.status_code == 422
        async with studio_http.store.factory() as session:
            stored = await session.get(StudioRun, run_id)
            decisions = await session.scalars(
                select(StudioDecision).where(StudioDecision.run_id == run_id)
            )
            assert stored is not None and stored.revision == 1 and stored.status == "idea_review"
            assert list(decisions) == []

        feedback = {
            "replace_ordinals": [2, 5],
            "reason": "unclear_action",
            "note": " Make the action clear. ",
        }
        revised = await owner.post(
            f"/v1/studio/runs/{run_id}/decisions",
            headers=headers,
            json={**base_payload, "revision_feedback": feedback},
        )
        assert revised.status_code == 200
        assert revised.json()["revision"] == 2 and revised.json()["status"] == "ideating"
        replay = await owner.post(
            f"/v1/studio/runs/{run_id}/decisions",
            headers=headers,
            json={**base_payload, "revision_feedback": feedback},
        )
        assert replay.status_code == 200 and replay.json()["revision"] == 2
        conflict = await owner.post(
            f"/v1/studio/runs/{run_id}/decisions",
            headers=headers,
            json={
                **base_payload,
                "revision_feedback": {**feedback, "replace_ordinals": [3]},
            },
        )
        assert conflict.status_code == 409 and conflict.json() == {"error": "idempotency_conflict"}
        async with studio_http.store.factory() as session:
            decision = await session.scalar(
                select(StudioDecision).where(
                    StudioDecision.run_id == run_id,
                    StudioDecision.request_key == "creative-v2-revise",
                )
            )
            assert decision is not None
            assert decision.payload["previous_plan"] == plan.model_dump(mode="json")
            assert decision.payload["revision_feedback"] == {
                "replace_ordinals": [2, 5],
                "reason": "unclear_action",
                "note": "Make the action clear.",
            }


@pytest.mark.asyncio
async def test_shared_preview_requires_owner_or_read_capability(
    studio_http: StudioHttp,
) -> None:
    async with _client(studio_http.app) as owner, _client(studio_http.app) as other:
        owner_headers = await _session(owner, studio_http.store.config.session_cookie_name)
        other_headers = await _session(other, studio_http.store.config.session_cookie_name)
        created = await owner.post(
            "/v1/studio/runs",
            headers=owner_headers,
            json={"item": "teacup", "request_key": "private-preview"},
        )
        run_id = UUID(created.json()["id"])
        path = studio_http.store.config.media_root / str(run_id) / "final.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"verified-local-preview")
        async with studio_http.store.factory.begin() as session:
            run = await session.get(StudioRun, run_id)
            assert run is not None
            run.final_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            run.final_metadata = {"media_verified": True}

        owner_preview = await owner.get(f"/v1/studio/runs/{run_id}/preview")
        assert owner_preview.status_code == 200 and owner_preview.content == path.read_bytes()
        assert (await other.get(f"/v1/studio/runs/{run_id}/preview")).status_code == 404
        share = studio_http.store.share_token(run_id)
        shared = await other.get(f"/v1/studio/runs/{run_id}/preview?share={share}")
        assert shared.status_code == 200 and shared.content == path.read_bytes()
        no_authority = await other.post(
            f"/v1/studio/runs/{run_id}/decisions?share={share}",
            headers=other_headers,
            json={
                "gate": "ideas",
                "decision": "approve",
                "revision": 1,
                "subject_hash": "a" * 64,
                "request_key": "share-no-action",
            },
        )
        assert no_authority.status_code == 404 and no_authority.json() == {"error": "run_not_found"}


@pytest.mark.asyncio
async def test_renderer_callback_checks_hmac_job_binding_and_duplicate(
    studio_http: StudioHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "remotion_webhook_secret", "local-renderer-secret")
    async with _client(studio_http.app) as owner:
        headers = await _session(owner, studio_http.store.config.session_cookie_name)
        created = await owner.post(
            "/v1/studio/runs",
            headers=headers,
            json={"item": "teacup", "request_key": "renderer-callback"},
        )
        run_id = UUID(created.json()["id"])
        async with studio_http.store.factory.begin() as session:
            run = await session.get(StudioRun, run_id)
            assert run is not None
            run.status, run.render_job_id, run.render_input_hash = (
                "editing",
                "render-expected",
                "input-expected",
            )

        body = json.dumps(
            {
                "run_id": str(run_id),
                "status": "done",
                "job_id": "render-expected",
                "input_hash": "input-expected",
            }
        ).encode()
        invalid = await owner.post(
            f"/v1/studio/callbacks/remotion/{run_id}",
            content=body,
            headers={"X-Remotion-Signature": "sha512=wrong"},
        )
        assert invalid.status_code == 401 and invalid.json() == {
            "error": "callback_signature_invalid"
        }
        signature = "sha512=" + hmac.new(b"local-renderer-secret", body, "sha512").hexdigest()
        wrong_job_body = json.dumps(
            {
                "run_id": str(run_id),
                "status": "done",
                "job_id": "wrong-job",
                "input_hash": "input-expected",
            }
        ).encode()
        wrong_job_signature = (
            "sha512=" + hmac.new(b"local-renderer-secret", wrong_job_body, "sha512").hexdigest()
        )
        mismatch = await owner.post(
            f"/v1/studio/callbacks/remotion/{run_id}",
            content=wrong_job_body,
            headers={"X-Remotion-Signature": wrong_job_signature},
        )
        assert mismatch.status_code == 409 and mismatch.json() == {"error": "callback_job_mismatch"}
        accepted = await owner.post(
            f"/v1/studio/callbacks/remotion/{run_id}",
            content=body,
            headers={"X-Remotion-Signature": signature},
        )
        assert accepted.json() == {"accepted": True, "duplicate": False}
        duplicate = await owner.post(
            f"/v1/studio/callbacks/remotion/{run_id}",
            content=body,
            headers={"X-Remotion-Signature": signature},
        )
        assert duplicate.json() == {"accepted": True, "duplicate": True}
