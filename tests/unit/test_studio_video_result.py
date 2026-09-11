"""Visitor video completion never creates publication authority or effects."""

from decimal import Decimal
from hashlib import sha256

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.api.routes.studio import get_store, router
from myloware.config.studio import StudioSettings
from myloware.storage.models import Base
from myloware.storage.studio_models import (
    StudioDecision,
    StudioGalleryProjectionIntent,
    StudioReservation,
    StudioRun,
)
from myloware.storage.studio_store import TERMINAL, StudioError, StudioStore
from myloware.workflows.monthly import deterministic_fixture_plan


async def make_store(tmp_path, *, mode="fixture"):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'result.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = StudioSettings(
        enabled=True,
        media_root=tmp_path / "media",
        mode=mode,
        recorded_root=tmp_path if mode == "recorded" else None,
        active_runs=1,
        visitor_runs_24h=3,
        daily_budget_usd=3,
        run_reservation_usd=1,
        cost_profile_version="test-v1" if mode == "live" else None,
        cost_input_moderation_usd=Decimal("0.01") if mode == "live" else None,
        cost_ideation_usd=Decimal("0.01") if mode == "live" else None,
        cost_plan_moderation_usd=Decimal("0.01") if mode == "live" else None,
        cost_video_request_usd=Decimal("0.01") if mode == "live" else None,
        cost_narration_batch_usd=Decimal("0.01") if mode == "live" else None,
        cost_render_usd=Decimal("0.01") if mode == "live" else None,
        cost_final_moderation_usd=Decimal("0.01") if mode == "live" else None,
    )
    return StudioStore(async_sessionmaker(engine, expire_on_commit=False), config), engine


async def final_run(store):
    visitor, cookie = await store.new_visitor("127.0.0.1")
    run_id = await store.admit(
        visitor_id=visitor.id, item_text="Teacup", start_key="start-result", ip="127.0.0.1"
    )
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        run.status = "final_review"
        run.final_hash = "a" * 64
        run.final_metadata = {"media_verified": True}
        await store.event(session, run, "final_review", "render_verified")
    return visitor, cookie, run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fixture", "recorded", "live"])
async def test_completion_is_terminal_releases_slot_and_preserves_unknown_live_cost(tmp_path, mode):
    store, engine = await make_store(tmp_path, mode=mode)
    try:
        owner, _, run_id = await final_run(store)
        reviewed = await store.snapshot(run_id, visitor_id=owner.id)
        args = {
            "run_id": run_id,
            "visitor_id": owner.id,
            "gate": "final",
            "revision": 1,
            "subject_hash": reviewed["final"]["review_hash"],
            "decision": "approve",
            "decision_key": "finish-result",
        }
        await store.decide(**args)
        await store.decide(**args)
        run = await store.get_run(run_id)
        assert run.status == "video_complete" and run.status in TERMINAL
        assert run.approved_publish_hash is None and run.publish_state is None
        result = await store.snapshot(run_id, visitor_id=owner.id)
        assert not result["can_act"] and "publish" not in result
        event_types = [event["type"] for event in result["events"]]
        assert event_types.index("render_verified") < event_types.index("video_completed")
        assert "visitor accepted" in result["events"][-1]["explanation"].lower()
        async with store.factory() as session:
            decisions = (await session.scalars(select(StudioDecision))).all()
            assert len(decisions) == 1 and decisions[0].gate == "final"
            reservation = await session.get(StudioReservation, run_id)
            if mode == "live":
                assert reservation.closed_at is None and reservation.confirmed_usd is None
                assert reservation.reserved_usd > Decimal(0)
            else:
                assert reservation.closed_at is not None and reservation.confirmed_usd == 0
        # A finished run no longer consumes the single active workflow slot.
        await store.admit(
            visitor_id=owner.id, item_text="Teacup", start_key="next-result", ip="127.0.0.1"
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_final_gate_binds_visitor_revision_bytes_and_verified_media(tmp_path):
    store, engine = await make_store(tmp_path)
    try:
        owner, _, run_id = await final_run(store)
        other, _ = await store.new_visitor("127.0.0.2")
        review_hash = store.final_review_hash(await store.get_run(run_id))
        args = {
            "run_id": run_id,
            "visitor_id": owner.id,
            "gate": "final",
            "revision": 1,
            "subject_hash": review_hash,
            "decision": "approve",
            "decision_key": "finish-result",
        }
        with pytest.raises(StudioError, match="run_not_found"):
            await store.decide(**{**args, "visitor_id": other.id})
        with pytest.raises(StudioError, match="stale_review"):
            await store.decide(**{**args, "revision": 2})
        with pytest.raises(StudioError, match="invalid_decision"):
            await store.decide(**{**args, "gate": "publish"})
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            run.final_hash = "b" * 64
        with pytest.raises(StudioError, match="stale_review"):
            await store.decide(**args)
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            run.final_hash = "a" * 64
            run.final_metadata = {"media_verified": False}
        with pytest.raises(StudioError, match="stale_review"):
            await store.decide(**args)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_gallery_opt_in_requires_exact_receipts_and_commits_source_intent(tmp_path):
    store, engine = await make_store(tmp_path, mode="recorded")
    try:
        owner, _, run_id = await final_run(store)
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            assert run is not None
            run.final_metadata = {"media_verified": True, "sha256": run.final_hash}
        args = {
            "run_id": run_id,
            "visitor_id": owner.id,
            "gate": "final",
            "revision": 1,
            "subject_hash": store.final_review_hash(await store.get_run(run_id)),
            "decision": "approve",
            "decision_key": "gallery-exact-final",
            "publish_to_gallery": True,
            "visibility_version": "recent-creations-v1",
        }
        with pytest.raises(StudioError, match="gallery_eligibility_unverified"):
            await store.decide(**args)
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            assert run is not None
            run.final_metadata = {
                "media_verified": True,
                "sha256": run.final_hash,
                "public_suitability": {
                    "status": "passed",
                    "final_hash": run.final_hash,
                    "receipt": "suitability-final-a",
                    "expires_at": "2999-01-01T00:00:00+00:00",
                },
                "public_rights": {
                    "status": "passed",
                    "final_hash": run.final_hash,
                    "receipt": "rights-final-a",
                    "profile_expires_at": "2999-01-01T00:00:00+00:00",
                    "profile_version": "test-v1",
                    "profile_receipt_id": "test-rights",
                },
            }
        await store.decide(**args)
        async with store.factory() as session:
            intents = (await session.scalars(select(StudioGalleryProjectionIntent))).all()
            assert len(intents) == 1
            assert intents[0].final_hash == "a" * 64 and intents[0].state == "pending"
            decision = await session.scalar(select(StudioDecision))
            assert (
                decision
                and decision.payload["gallery_consent"]["visibility_version"]
                == "recent-creations-v1"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fixture_final_can_be_accepted_without_public_gallery(tmp_path):
    store, engine = await make_store(tmp_path, mode="fixture")
    try:
        owner, _, run_id = await final_run(store)
        await store.decide(
            run_id=run_id,
            visitor_id=owner.id,
            gate="final",
            revision=1,
            subject_hash=store.final_review_hash(await store.get_run(run_id)),
            decision="approve",
            decision_key="fixture-private-final",
        )
        async with store.factory() as session:
            assert await session.scalar(select(StudioGalleryProjectionIntent)) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_http_rejects_publication_and_share_cannot_complete(tmp_path):
    store, engine = await make_store(tmp_path)
    try:
        owner, cookie, run_id = await final_run(store)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_store] = lambda: store
        payload = {
            "gate": "publish",
            "decision": "approve",
            "revision": 1,
            "subject_hash": store.final_review_hash(await store.get_run(run_id)),
            "request_key": "finish-result",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=store.config.origin
        ) as client:
            response = await client.post(
                f"/v1/studio/runs/{run_id}/decisions",
                json=payload,
                headers={
                    "origin": store.config.origin,
                    "x-csrf-token": store.csrf(owner.id),
                    "cookie": f"aismr_session={cookie}",
                },
            )
            assert response.status_code == 422
            payload["gate"] = "final"
            response = await client.post(
                f"/v1/studio/runs/{run_id}/decisions?share={store.share_token(run_id)}",
                json=payload,
                headers={"origin": store.config.origin},
            )
            assert response.status_code == 401
            assert (await store.get_run(run_id)).status == "final_review"
    finally:
        await engine.dispose()


def test_live_readiness_has_no_publisher_dependency():
    config = StudioSettings(
        enabled=True,
        mode="live",
        live_enabled=True,
        FAL_API_KEY="test",
        OPENAI_API_KEY="test",
        session_secret="s" * 32,
        daily_budget_usd=1,
        run_reservation_usd=1,
        render_real=True,
    )
    assert config.live_configuration_errors() == ()


@pytest.mark.asyncio
async def test_matching_bytes_without_verification_are_never_previewed(tmp_path):
    store, engine = await make_store(tmp_path)
    try:
        owner, cookie, run_id = await final_run(store)
        path = store.config.media_root / str(run_id) / "final.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"offline-test-video")
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            run.final_hash = sha256(path.read_bytes()).hexdigest()
            run.final_metadata = {"media_verified": False}
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_store] = lambda: store
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=store.config.origin
        ) as client:
            headers = {"cookie": f"aismr_session={cookie}"}
            result = await store.snapshot(run_id, visitor_id=owner.id)
            assert result["final"] is None
            assert (
                await client.get(f"/v1/studio/runs/{run_id}/preview", headers=headers)
            ).status_code == 404
            async with store.transaction() as session:
                run = await session.get(StudioRun, run_id)
                run.final_metadata = {"media_verified": True}
            response = await client.get(f"/v1/studio/runs/{run_id}/preview", headers=headers)
            assert response.status_code == 200 and response.content == path.read_bytes()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recorded_mode_refuses_new_item_and_revision(tmp_path):
    store, engine = await make_store(tmp_path, mode="recorded")
    try:
        owner, _ = await store.new_visitor("127.0.0.1")
        with pytest.raises(StudioError, match="recorded_item_required"):
            await store.admit(
                visitor_id=owner.id, item_text="chair", start_key="recorded-key", ip="127.0.0.1"
            )
        run_id = await store.admit(
            visitor_id=owner.id, item_text="Teacup", start_key="recorded-key", ip="127.0.0.1"
        )
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            plan = deterministic_fixture_plan(run_id, "teacup")
            run.status = "idea_review"
            run.plan = plan.model_dump(mode="json")
            run.plan_hash = plan.canonical_sha256
        with pytest.raises(StudioError, match="revision_allowance_exhausted"):
            await store.decide(
                run_id=run_id,
                visitor_id=owner.id,
                gate="ideas",
                revision=1,
                subject_hash=plan.canonical_sha256,
                decision="revise",
                decision_key="revise-recorded",
            )
    finally:
        await engine.dispose()
