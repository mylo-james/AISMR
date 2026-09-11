"""PostgreSQL-only proofs for AISMR's database coordination boundaries.

Run this file with ``AISMR_TEST_POSTGRES_URL`` pointing to a disposable test
database. Each test session receives a generated schema that is dropped at
teardown, so the selected database's existing schema is never modified.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from myloware.config.studio import StudioSettings
from myloware.storage.models import Base, Job, _utc_now
from myloware.storage.repositories import JobRepository, StaleJobClaim
from myloware.storage.studio_models import StudioRun
from myloware.storage.studio_store import StudioError, StudioStore
from myloware.workflows.scenes import deterministic_scene_plan

pytestmark = [pytest.mark.integration, pytest.mark.parity]


def _async_postgres_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgresql+psycopg2://")
    return url


@dataclass
class ParityDatabase:
    engine: AsyncEngine
    factory: async_sessionmaker


@pytest.fixture
async def postgres_parity_db() -> ParityDatabase:
    configured_url = os.environ.get("AISMR_TEST_POSTGRES_URL")
    if not configured_url:
        pytest.skip("AISMR_TEST_POSTGRES_URL is not set; PostgreSQL parity is opt-in")

    url = _async_postgres_url(configured_url)
    schema = f"aismr_parity_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine: AsyncEngine | None = None
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield ParityDatabase(
            engine=engine, factory=async_sessionmaker(engine, expire_on_commit=False)
        )
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


def _store(factory: async_sessionmaker, **overrides: object) -> StudioStore:
    settings = StudioSettings(
        **{
            "enabled": True,
            "active_runs": 1,
            "visitor_runs_24h": 3,
            "ip_runs_24h": 3,
            **overrides,
        }
    )
    return StudioStore(factory, settings)


@pytest.mark.asyncio
async def test_postgres_admission_serializes_contended_starts(
    postgres_parity_db: ParityDatabase,
) -> None:
    store = _store(postgres_parity_db.factory)
    first, _ = await store.new_visitor("127.0.0.1")
    second, _ = await store.new_visitor("127.0.0.2")

    results = await asyncio.gather(
        store.admit(
            visitor_id=first.id,
            item_text="teacup",
            start_key="postgres-start-1",
            ip="127.0.0.1",
        ),
        store.admit(
            visitor_id=second.id,
            item_text="teacup",
            start_key="postgres-start-2",
            ip="127.0.0.2",
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    rejected = next(result for result in results if isinstance(result, Exception))
    assert isinstance(rejected, StudioError)
    assert rejected.code == "studio_busy"


@pytest.mark.asyncio
async def test_postgres_decision_remains_bound_to_owner_revision_and_hash(
    postgres_parity_db: ParityDatabase,
) -> None:
    store = _store(postgres_parity_db.factory, active_runs=2)
    owner, _ = await store.new_visitor("127.0.0.1")
    other, _ = await store.new_visitor("127.0.0.2")
    run_id = await store.admit(
        visitor_id=owner.id,
        item_text="teacup",
        start_key="postgres-decision",
        ip="127.0.0.1",
    )
    plan = deterministic_scene_plan(run_id, "teacup")
    subject_hash = plan.canonical_sha256
    async with postgres_parity_db.factory.begin() as session:
        run = await session.get(StudioRun, run_id)
        assert run is not None
        run.status = "idea_review"
        run.plan = plan.model_dump(mode="json")
        run.plan_hash = subject_hash

    with pytest.raises(StudioError, match="run_not_found"):
        await store.decide(
            run_id=run_id,
            visitor_id=other.id,
            gate="ideas",
            decision="approve",
            revision=1,
            subject_hash=subject_hash,
            decision_key="other-decision",
        )
    with pytest.raises(StudioError, match="stale_review"):
        await store.decide(
            run_id=run_id,
            visitor_id=owner.id,
            gate="ideas",
            decision="approve",
            revision=2,
            subject_hash=subject_hash,
            decision_key="stale-revision",
        )

    await store.decide(
        run_id=run_id,
        visitor_id=owner.id,
        gate="ideas",
        decision="approve",
        revision=1,
        subject_hash=subject_hash,
        decision_key="owner-decision",
    )
    assert (await store.get_run(run_id)).plan_approved_hash == subject_hash


@pytest.mark.asyncio
async def test_postgres_claim_fencing_rejects_reused_worker_id(
    postgres_parity_db: ParityDatabase,
) -> None:
    async with postgres_parity_db.factory.begin() as session:
        job = await JobRepository(session).enqueue_async(
            "studio.advance", idempotency_key="postgres-claim"
        )
        job_id = job.id

    async with postgres_parity_db.factory.begin() as session:
        first = await JobRepository(session).claim_next_async(worker_id="same-worker")
        assert first is not None
        assert first.claim_generation == 1

    async with postgres_parity_db.factory.begin() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=_utc_now() - timedelta(seconds=1))
        )

    async with postgres_parity_db.factory.begin() as session:
        replacement = await JobRepository(session).claim_next_async(worker_id="same-worker")
        assert replacement is not None
        assert replacement.id == job_id
        assert replacement.claim_generation == 2

    async with postgres_parity_db.factory.begin() as session:
        with pytest.raises(StaleJobClaim):
            await JobRepository(session).mark_succeeded_async(
                job_id, worker_id="same-worker", claim_generation=1
            )
