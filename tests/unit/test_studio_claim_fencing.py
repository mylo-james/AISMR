from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.config.studio import StudioSettings
from myloware.storage.models import Base, Job
from myloware.storage.repositories import JobRepository, StaleJobClaim
from myloware.storage.studio_models import StudioReservation
from myloware.storage.studio_store import StudioStore
from myloware.studio.moderation import ModerationVerdict
from myloware.studio.service import StudioService
from myloware.workers.claims import JobClaim, current_job_claim
from myloware.workflows.monthly import deterministic_fixture_plan


class _SafeModerator:
    async def moderate_text(self, _text: str, *, stage: str) -> ModerationVerdict:
        return ModerationVerdict(True, "safe", "test", (f"text:{stage}",))

    async def moderate_images(self, _frames, *, stage: str) -> ModerationVerdict:  # type: ignore[no-untyped-def]
        return ModerationVerdict(True, "safe", "test", (f"image:{stage}",))


class _BlockingIdeator:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def create_plan(self, *, run_id, revision: int, item: str):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return deterministic_fixture_plan(run_id, item, revision)


async def _store(tmp_path, *, mode: str = "fixture", **overrides):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'claim-fence.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    values = {
        "enabled": True,
        "mode": mode,
        "active_runs": 2,
        "visitor_runs_24h": 1,
        "ip_runs_24h": 1,
        **overrides,
    }
    if mode == "live":
        values.update(
            {
                "fal_key": "f",
                "zernio_key": "z",
                "tiktok_account_id": "account",
                "daily_budget_usd": Decimal(1),
                "run_reservation_usd": Decimal("0.1"),
                "session_secret": "x" * 32,
                "render_real": True,
                "cost_profile_version": "test-v1",
                "cost_input_moderation_usd": "0.001",
                "cost_ideation_usd": "0.001",
                "cost_plan_moderation_usd": "0.001",
                "cost_video_request_usd": "0.001",
                "cost_narration_batch_usd": "0.001",
                "cost_render_usd": "0.001",
                "cost_final_moderation_usd": "0.001",
            }
        )
    return (
        StudioStore(async_sessionmaker(engine, expire_on_commit=False), StudioSettings(**values)),
        engine,
    )


@pytest.mark.asyncio
async def test_reclaimed_claim_fences_stale_ideation_and_prevents_second_ideator_call(
    tmp_path,
) -> None:
    store, engine = await _store(tmp_path, mode="live")
    try:
        visitor, _ = await store.new_visitor("127.0.0.1")
        run_id = await store.admit(
            visitor_id=visitor.id, item_text="chair", start_key="claim-fence-1", ip="127.0.0.1"
        )
        async with store.factory() as session:
            jobs = JobRepository(session)
            first = await jobs.claim_next_async(worker_id="same-worker", lease_seconds=60)
            assert first is not None
            await session.commit()
        ideator = _BlockingIdeator()
        service = StudioService(store, moderator=_SafeModerator(), ideator=ideator)  # type: ignore[arg-type]
        token = current_job_claim.set(JobClaim(first.id, "same-worker", first.claim_generation))
        try:
            stale_task = asyncio.create_task(service.ideate(run_id))
        finally:
            current_job_claim.reset(token)
        await ideator.started.wait()
        async with store.factory() as session:
            jobs = JobRepository(session)
            job = await session.get(Job, first.id)
            assert job is not None
            job.lease_expires_at = jobs._utc_now_naive() - timedelta(seconds=1)
            await session.commit()
            second = await jobs.claim_next_async(worker_id="same-worker", lease_seconds=60)
            assert second is not None and second.claim_generation == first.claim_generation + 1
            await session.commit()
        ideator.release.set()
        with pytest.raises(StaleJobClaim, match="monthly stage no longer owns"):
            await stale_task
        token = current_job_claim.set(JobClaim(second.id, "same-worker", second.claim_generation))
        try:
            await service.ideate(run_id)
        finally:
            current_job_claim.reset(token)
        run = await store.get_run(run_id)
        assert run.status == "submission_unknown"
        assert ideator.calls == 1
    finally:
        await engine.dispose()


class _InputDenyModerator(_SafeModerator):
    async def moderate_text(self, text: str, *, stage: str) -> ModerationVerdict:
        if stage == "input":
            return ModerationVerdict(False, "denied", "test", ("text:input",))
        return await super().moderate_text(text, stage=stage)


@pytest.mark.asyncio
async def test_input_moderation_rejection_does_not_consume_visitor_or_ip_quota(tmp_path) -> None:
    store, engine = await _store(tmp_path)
    try:
        visitor, _ = await store.new_visitor("127.0.0.1")
        run_id = await store.admit(
            visitor_id=visitor.id, item_text="chair", start_key="quota-deny-1", ip="127.0.0.1"
        )
        await StudioService(store, moderator=_InputDenyModerator()).ideate(run_id)
        async with store.factory() as session:
            reservation = await session.scalar(
                select(StudioReservation).where(StudioReservation.run_id == run_id)
            )
            assert reservation is not None and reservation.cost_state == "input_rejected"
        await store.admit(
            visitor_id=visitor.id, item_text="chair", start_key="quota-deny-2", ip="127.0.0.1"
        )
    finally:
        await engine.dispose()
