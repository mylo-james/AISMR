from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.config.studio import StudioSettings
from myloware.storage.models import Base, Job, Run
from myloware.storage.studio_models import (
    StudioAdmissionControl,
    StudioCostLedger,
    StudioEvent,
    StudioReservation,
    StudioRun,
)
from myloware.storage.studio_store import StudioStore
from myloware.studio.housekeeping import run_studio_housekeeping

NOW = datetime(2026, 9, 9, tzinfo=UTC)


async def _store(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'housekeeping.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return (
        StudioStore(
            async_sessionmaker(engine, expire_on_commit=False),
            StudioSettings(
                enabled=True, media_root=tmp_path / "media", fixture_root=tmp_path / "fixtures"
            ),
        ),
        engine,
    )


async def _run(
    store: StudioStore, *, status: str, expires_at: datetime, verified_final: bool = False
):
    run_id = uuid4()
    async with store.transaction() as session:
        session.add(Run(id=run_id, workflow_name="monthly", input="teacup", status="pending"))
        await session.flush()
        metadata = None
        final_hash = None
        if verified_final:
            final = store.config.media_root / str(run_id) / "final.mp4"
            metadata = {"media_verified": True, "path": str(final.resolve())}
            final_hash = "a" * 64
        session.add(
            StudioRun(
                run_id=run_id,
                visitor_id="visitor",
                start_key=f"key-{run_id}",
                read_token_hash=run_id.hex * 2,
                item_id="teacup",
                item_text="teacup",
                mode="fixture",
                status=status,
                final_hash=final_hash,
                final_metadata=metadata,
                plan_review_expires_at=expires_at if status == "idea_review" else None,
                final_review_expires_at=expires_at if status == "final_review" else None,
                expires_at=NOW + timedelta(days=1),
            )
        )
    return run_id


@pytest.mark.asyncio
async def test_housekeeping_expires_each_review_clock_and_queues_only_owned_final(tmp_path) -> None:
    store, engine = await _store(tmp_path)
    try:
        plan_id = await _run(store, status="idea_review", expires_at=NOW - timedelta(seconds=1))
        final_id = await _run(
            store, status="final_review", expires_at=NOW - timedelta(seconds=1), verified_final=True
        )
        unowned_final_id = await _run(
            store, status="final_review", expires_at=NOW - timedelta(seconds=1)
        )
        receipt = await run_studio_housekeeping(store, now=NOW)
        assert receipt.expired_plan_reviews == (plan_id,)
        assert receipt.expired_final_reviews == (final_id, unowned_final_id)
        assert receipt.cleanup_queued == (final_id,)
        async with store.factory() as session:
            saved = await session.get(StudioRun, plan_id)
            assert saved.status == "cancelled" and saved.error_code == "plan_review_expired"
            assert await session.scalar(select(Job).where(Job.run_id == final_id)) is not None
            assert await session.scalar(select(Job).where(Job.run_id == unowned_final_id)) is None
            events = (
                await session.scalars(select(StudioEvent).where(StudioEvent.run_id == final_id))
            ).all()
            assert any(event.event_type == "review_expired" for event in events)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_housekeeping_retains_overdue_unknown_cost_and_pauses_admission(tmp_path) -> None:
    store, engine = await _store(tmp_path)
    try:
        run_id = await _run(
            store, status="final_review", expires_at=NOW - timedelta(seconds=1), verified_final=True
        )
        ledger_id = uuid4()
        async with store.transaction() as session:
            session.add(
                StudioCostLedger(
                    id=ledger_id,
                    run_id=run_id,
                    profile_version="owner-v1",
                    stage="render",
                    operation_key="initial",
                    reserved_usd=Decimal(1),
                    cost_state="unknown",
                    created_at=NOW - timedelta(hours=49),
                )
            )
        receipt = await run_studio_housekeeping(store, now=NOW)
        assert receipt.overdue_unknown_cost_entries == (ledger_id,)
        assert receipt.cleanup_deferred_for_billable_uncertainty == (run_id,)
        assert receipt.cleanup_queued == ()
        async with store.factory() as session:
            control = await session.get(StudioAdmissionControl, 1)
            assert control is not None and control.paused is True
            assert control.reason == "operator_reconciliation_required"
            assert await session.scalar(select(Job).where(Job.run_id == run_id)) is None
            events = (
                await session.scalars(select(StudioEvent).where(StudioEvent.run_id == run_id))
            ).all()
            assert {event.event_type for event in events} >= {
                "review_expired",
                "cleanup_deferred_billable_uncertainty",
                "cost_reconciliation_overdue",
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_housekeeping_defers_cleanup_for_legacy_unknown_reservation(tmp_path) -> None:
    store, engine = await _store(tmp_path)
    try:
        run_id = await _run(
            store, status="final_review", expires_at=NOW - timedelta(seconds=1), verified_final=True
        )
        async with store.transaction() as session:
            session.add(
                StudioReservation(
                    run_id=run_id,
                    visitor_id="visitor",
                    ip_hash="ip",
                    reserved_usd=Decimal(1),
                    cost_state="submission_unknown",
                    created_at=NOW - timedelta(hours=49),
                )
            )
        receipt = await run_studio_housekeeping(store, now=NOW)
        assert receipt.overdue_unknown_cost_entries == (run_id,)
        assert receipt.cleanup_deferred_for_billable_uncertainty == (run_id,)
        async with store.factory() as session:
            assert await session.scalar(select(Job).where(Job.run_id == run_id)) is None
    finally:
        await engine.dispose()
