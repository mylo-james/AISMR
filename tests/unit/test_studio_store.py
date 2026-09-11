from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.config.studio import StudioSettings
from myloware.storage.models import Base, _utc_now
from myloware.storage.studio_models import StudioReservation, StudioRun, StudioVisitor
from myloware.storage.studio_store import StudioError, StudioStore


async def _store(tmp_path, **overrides):  # type: ignore[no-untyped-def]
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'studio.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = StudioSettings(
        **{
            "enabled": True,
            "active_runs": 1,
            "visitor_runs_24h": 2,
            "ip_runs_24h": 4,
            **overrides,
        }
    )
    return (
        StudioStore(async_sessionmaker(engine, expire_on_commit=False), config),
        engine,
    )


@pytest.mark.asyncio
async def test_atomic_admission_and_idempotency_on_file_sqlite(tmp_path) -> None:
    store, engine = await _store(tmp_path)
    first, _ = await store.new_visitor("127.0.0.1")
    second, _ = await store.new_visitor("127.0.0.2")
    first_run = await store.admit(
        visitor_id=first.id, item_text="chair", start_key="start-key-1", ip="127.0.0.1"
    )
    assert first_run == await store.admit(
        visitor_id=first.id, item_text="chair", start_key="start-key-1", ip="127.0.0.1"
    )

    with pytest.raises(StudioError, match="studio_busy"):
        await store.admit(
            visitor_id=second.id,
            item_text="bed",
            start_key="start-key-2",
            ip="127.0.0.2",
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_fixture_admission_limits_remain_enforced(tmp_path) -> None:
    visitor_store, visitor_engine = await _store(
        tmp_path / "visitor", active_runs=20, visitor_runs_24h=1, ip_runs_24h=20
    )
    try:
        visitor, _ = await visitor_store.new_visitor("127.0.0.1")
        await visitor_store.admit(
            visitor_id=visitor.id, item_text="chair", start_key="visitor-limit-1", ip="127.0.0.1"
        )
        with pytest.raises(StudioError, match="visitor_allowance_exhausted"):
            await visitor_store.admit(
                visitor_id=visitor.id, item_text="bed", start_key="visitor-limit-2", ip="127.0.0.1"
            )
    finally:
        await visitor_engine.dispose()

    network_store, network_engine = await _store(
        tmp_path / "network", active_runs=20, visitor_runs_24h=20, ip_runs_24h=1
    )
    try:
        first, _ = await network_store.new_visitor("127.0.0.1")
        second, _ = await network_store.new_visitor("127.0.0.1")
        await network_store.admit(
            visitor_id=first.id, item_text="chair", start_key="network-limit-1", ip="127.0.0.1"
        )
        with pytest.raises(StudioError, match="network_allowance_exhausted"):
            await network_store.admit(
                visitor_id=second.id, item_text="bed", start_key="network-limit-2", ip="127.0.0.1"
            )
    finally:
        await network_engine.dispose()


@pytest.mark.asyncio
async def test_fixture_unlimited_admission_keeps_history_and_skips_only_admission_counts(
    tmp_path,
) -> None:
    store, engine = await _store(
        tmp_path,
        active_runs=1,
        visitor_runs_24h=1,
        ip_runs_24h=1,
        fixture_unlimited_admissions=True,
    )
    try:
        visitor, _ = await store.new_visitor("127.0.0.1")
        run_ids = [
            await store.admit(
                visitor_id=visitor.id,
                item_text=item,
                start_key=f"unlimited-{index}",
                ip="127.0.0.1",
            )
            for index, item in enumerate(("chair", "bed", "teacup"), start=1)
        ]
        async with store.factory() as session:
            persisted = (
                await session.scalars(
                    select(StudioRun.run_id)
                    .where(StudioRun.visitor_id == visitor.id)
                    .order_by(StudioRun.created_at)
                )
            ).all()
            assert set(persisted) == set(run_ids)
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(StudioReservation)
                    .where(StudioReservation.visitor_id == visitor.id)
                )
                == 3
            )
        store.config.admission_paused = True
        with pytest.raises(StudioError, match="admissions_paused"):
            await store.admit(
                visitor_id=visitor.id,
                item_text="lamp",
                start_key="unlimited-paused",
                ip="127.0.0.1",
            )
    finally:
        await engine.dispose()


def test_fixture_unlimited_admission_is_rejected_outside_fixture_mode(tmp_path) -> None:
    with pytest.raises(ValidationError, match="Fixture unlimited admissions require fixture mode"):
        StudioSettings(mode="live", fixture_unlimited_admissions=True)
    with pytest.raises(ValidationError, match="Fixture unlimited admissions require fixture mode"):
        StudioSettings(
            mode="recorded",
            recorded_root=tmp_path / "recorded",
            fixture_unlimited_admissions=True,
        )


@pytest.mark.asyncio
async def test_store_defensively_keeps_recorded_active_limit_if_config_is_mutated(tmp_path) -> None:
    store, engine = await _store(tmp_path, active_runs=1)
    try:
        # Settings validation prevents this configuration. The store condition
        # also protects an already-created settings object from mode mutation.
        store.config.mode = "recorded"
        store.config.fixture_unlimited_admissions = True
        first, _ = await store.new_visitor("127.0.0.1")
        second, _ = await store.new_visitor("127.0.0.2")
        await store.admit(
            visitor_id=first.id, item_text="teacup", start_key="recorded-guard-1", ip="127.0.0.1"
        )
        with pytest.raises(StudioError, match="studio_busy"):
            await store.admit(
                visitor_id=second.id,
                item_text="teacup",
                start_key="recorded-guard-2",
                ip="127.0.0.2",
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_share_is_read_only_and_cross_visitor_is_hidden(
    tmp_path,
) -> None:
    store, engine = await _store(tmp_path, active_runs=2)
    owner, _ = await store.new_visitor("127.0.0.1")
    other, _ = await store.new_visitor("127.0.0.2")
    run_id = await store.admit(
        visitor_id=owner.id, item_text="cake", start_key="start-key-1", ip="127.0.0.1"
    )
    with pytest.raises(StudioError, match="run_not_found"):
        await store.snapshot(run_id, visitor_id=other.id)
    shared = await store.snapshot(run_id, share=store.share_token(run_id))
    assert shared["can_act"] is False
    with pytest.raises(StudioError, match="run_not_found"):
        await store.decide(
            run_id=run_id,
            visitor_id=other.id,
            gate="ideas",
            decision="approve",
            revision=1,
            subject_hash="x",
            decision_key="decision-1",
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_session_and_stale_hash_fail_before_decision(tmp_path) -> None:
    store, engine = await _store(tmp_path)
    visitor, _ = await store.new_visitor("127.0.0.1")
    run_id = await store.admit(
        visitor_id=visitor.id, item_text="bed", start_key="start-key-1", ip="127.0.0.1"
    )
    async with store.factory() as session:
        run = await session.get(StudioRun, run_id)
        assert run is not None
        run.status, run.plan_hash = "idea_review", "expected-hash"
        await session.commit()
    with pytest.raises(StudioError, match="stale_review"):
        await store.decide(
            run_id=run_id,
            visitor_id=visitor.id,
            gate="ideas",
            decision="approve",
            revision=1,
            subject_hash="wrong-hash",
            decision_key="decision-1",
        )
    async with store.factory() as session:
        saved = await session.get(StudioVisitor, visitor.id)
        assert saved is not None
        saved.expires_at = _utc_now() - timedelta(seconds=1)
        await session.commit()
    with pytest.raises(StudioError, match="session_expired"):
        await store.decide(
            run_id=run_id,
            visitor_id=visitor.id,
            gate="ideas",
            decision="approve",
            revision=1,
            subject_hash="expected-hash",
            decision_key="decision-2",
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_simultaneous_starts_compete_for_one_database_slot(tmp_path) -> None:
    store, engine = await _store(tmp_path)
    try:
        visitors = [await store.new_visitor(f"127.0.0.{i+1}") for i in range(2)]
        outcomes = await asyncio.gather(
            *[
                store.admit(
                    visitor_id=visitor.id,
                    item_text="teacup",
                    start_key=f"parallel-{i}",
                    ip=f"127.0.0.{i+1}",
                )
                for i, (visitor, _) in enumerate(visitors)
            ],
            return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in outcomes) == 1
        failure = next(result for result in outcomes if isinstance(result, Exception))
        assert isinstance(failure, StudioError) and failure.code == "studio_busy"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repeat_decision_and_revision_hash_are_bound(tmp_path) -> None:
    from sqlalchemy import func, select

    from myloware.storage.studio_models import StudioDecision
    from myloware.workflows.scenes import deterministic_scene_plan as deterministic_fixture_plan

    store, engine = await _store(tmp_path)
    try:
        visitor, cookie = await store.new_visitor("127.0.0.1")
        run_id = await store.admit(
            visitor_id=visitor.id,
            item_text="jellyfish lantern",
            start_key="custom-item",
            ip="127.0.0.1",
        )
        plan = deterministic_fixture_plan(run_id, "jellyfish lantern")
        await store.save_plan(plan, {"fixture": True, "safe": True})
        args = {
            "run_id": run_id,
            "visitor_id": visitor.id,
            "gate": "ideas",
            "decision": "approve",
            "revision": 1,
            "subject_hash": plan.canonical_sha256,
            "decision_key": "decision-key",
        }
        await store.decide(**args)
        await store.decide(**args)
        assert (await store.get_run(run_id)).status == "generating"
        async with store.factory() as session:
            assert await session.scalar(select(func.count()).select_from(StudioDecision)) == 1
        with pytest.raises(StudioError, match="idempotency_conflict"):
            await store.decide(**{**args, "decision": "cancel"})
        assert (await store.visitor(cookie)).id == visitor.id
        with pytest.raises(StudioError):
            await store.visitor(cookie + "forged")
    finally:
        await engine.dispose()
