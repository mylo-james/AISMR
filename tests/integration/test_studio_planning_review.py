"""Real HTTP/SQLite authority checks for the private text-agent review mode."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.api.routes.studio import get_store, router
from myloware.config.studio import StudioSettings
from myloware.storage.models import Base
from myloware.storage.studio_models import StudioAsset, StudioDecision
from myloware.storage.studio_store import TERMINAL, StudioError, StudioStore
from myloware.studio.planning_store import DurablePlanning
from myloware.workflows.scenes import ScenePlan, deterministic_scene_plan

ORIGIN = "https://studio.private.ts.net:8451"


@pytest.fixture
async def planning_http(
    tmp_path: Path,
) -> AsyncIterator[tuple[StudioStore, httpx.AsyncClient, dict[str, str]]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'planning.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = StudioSettings(
        _env_file=None,
        enabled=True,
        mode="planning",
        origin=ORIGIN,
        cookie_secure=True,
        session_secret="a-private-test-session-secret-with-more-than-32-characters",
        OPENAI_API_KEY="test-moderation-only",
        planner_version="creative-v2",
        active_runs=1,
        visitor_runs_24h=20,
        ip_runs_24h=20,
        plan_revisions=2,
    )
    store = StudioStore(async_sessionmaker(engine, expire_on_commit=False), config)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: store
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            response = await client.post("/v1/studio/session", headers={"Origin": ORIGIN}, json={})
            assert response.status_code == 200
            session = response.json()
            yield store, client, {
                "Origin": ORIGIN,
                "X-CSRF-Token": session["csrf"],
            }
    finally:
        await engine.dispose()


async def _ready(
    store: StudioStore, client: httpx.AsyncClient, headers: dict[str, str]
) -> tuple[UUID, ScenePlan]:
    response = await client.post(
        "/v1/studio/runs",
        headers=headers,
        json={"item": "Chair", "request_key": "planning-test-start"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "planning"
    run_id = UUID(response.json()["id"])
    # This test supplies a plan only to exercise HTTP review transitions.
    plan = deterministic_scene_plan(run_id, "chair", 1)
    await store.save_plan(plan, {"test": "review-transition-only"})
    current = await client.get(f"/v1/studio/runs/{run_id}")
    assert current.status_code == 200
    current_plan = current.json()["plan"]
    assert current_plan["schema_version"] == 2
    assert "scenes" in current_plan and "ideas" not in current_plan
    assert all(
        set(scene) == {"ordinal", "title", "visual_prompt"} for scene in current_plan["scenes"]
    )
    return run_id, plan


@pytest.mark.asyncio
async def test_keep_ideas_is_terminal_and_cannot_generate_or_publish(
    planning_http: tuple[StudioStore, httpx.AsyncClient, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, client, headers = planning_http
    run_id, plan = await _ready(store, client, headers)
    body = {
        "gate": "ideas",
        "revision": 1,
        "subject_hash": plan.canonical_sha256,
        "decision": "approve",
        "request_key": "planning-keep-ideas",
    }
    denied = await client.post(f"/v1/studio/runs/{run_id}/decisions", json=body)
    assert denied.status_code == 403
    response = await client.post(f"/v1/studio/runs/{run_id}/decisions", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "plan_complete"
    assert response.json()["assets"] == []
    assert response.json()["final"] is None
    assert "plan_complete" in TERMINAL
    assert (await client.get("/v1/studio/gallery")).json() == {"items": []}
    assert (await client.get(f"/v1/studio/gallery/{run_id}/video")).status_code == 404
    async with store.factory() as session:
        assert await session.scalar(select(func.count()).select_from(StudioAsset)) == 0
        decision = await session.scalar(select(StudioDecision))
        assert decision.decision == "approve"
    final = await client.post(
        f"/v1/studio/runs/{run_id}/decisions",
        headers=headers,
        json={**body, "gate": "final", "request_key": "planning-no-final"},
    )
    assert final.status_code == 409 and final.json()["error"] == "planning_only"

    from myloware.workflows.langgraph import studio as graph

    service = SimpleNamespace(
        store=store,
        assets=lambda _: pytest.fail("planning run reached media construction"),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(graph, "build_studio_service", lambda: service)
    with pytest.raises(StudioError, match="planning_only"):
        await graph.generate({"run_id": str(run_id), "status": "generating"})


@pytest.mark.asyncio
async def test_planning_revision_retains_unselected_ideas_and_reviews_free_worker_slots(
    planning_http: tuple[StudioStore, httpx.AsyncClient, dict[str, str]],
) -> None:
    store, client, headers = planning_http
    run_id, plan = await _ready(store, client, headers)
    # A ready review does not consume the sole active text-worker slot.
    another = await client.post(
        "/v1/studio/runs",
        headers=headers,
        json={"item": "Teacup", "request_key": "planning-second-item"},
    )
    assert another.status_code == 200
    response = await client.post(
        f"/v1/studio/runs/{run_id}/decisions",
        headers=headers,
        json={
            "gate": "ideas",
            "revision": 1,
            "subject_hash": plan.canonical_sha256,
            "decision": "revise",
            "request_key": "planning-revise-selected",
            "revision_feedback": {
                "replace_ordinals": [2, 3, 9],
                "reason": "repetitive",
                "note": "Use different visible events for these three scenes.",
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ideating"
    assert response.json()["revision"] == 2
    context = await DurablePlanning(store, run_id, 2).context()
    assert len(context.retained_ideas) == 9
    assert all(entry.idea == plan.ideas[entry.ordinal - 1] for entry in context.retained_ideas)
    assert (
        context.revision_feedback[0].note == "Use different visible events for these three scenes."
    )
