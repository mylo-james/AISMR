"""Bounded Vercel wake-ups for the existing SQL jobs and LangGraph checkpoints."""

from __future__ import annotations

import asyncio
import time
from uuid import UUID, uuid4

from sqlalchemy import select
from vercel.queue import Message, RetryAfter, send, subscribe

from myloware.config.studio import get_studio_settings
from myloware.observability.logging import get_logger
from myloware.storage.database import get_async_session_factory
from myloware.storage.models import Job
from myloware.storage.repositories import JobRepository, StaleJobClaim
from myloware.storage.studio_store import TERMINAL, StudioError, StudioStore
from myloware.studio.hosted_recorded import recorded_bundle
from myloware.workers.claims import JobClaim, current_job_claim

TOPIC = "aismr-recorded"
WORKING = {"ideating", "generating", "editing"}
logger = get_logger(__name__)


async def signal_run(store: StudioStore, run_id: UUID) -> None:
    run = await store.get_run(run_id)
    if run.status not in WORKING | TERMINAL:
        return
    async with store.factory() as session:
        job = await session.scalar(
            select(Job)
            .where(
                Job.run_id == run_id,
                Job.job_type.in_(("studio.advance", "studio.cleanup")),
                Job.status.in_(("pending", "running")),
            )
            .order_by(Job.created_at)
            .limit(1)
        )
        if job is None:
            return
        # SQL owns recovery across deployments and lost after-commit wake-ups.
        key = f"{job.id}:{job.claim_generation}:{run.status}:{int(time.time()) // 60}"
    await send(TOPIC, {"run_id": str(run_id)}, idempotency_key=key, retention=3600)


async def process_run(run_id: UUID) -> bool:
    """Claim one SQL job and finish a bounded batch of immediate recorded steps."""
    from myloware.workflows.langgraph.studio import advance_monthly_workflow

    config = get_studio_settings()
    recorded_bundle(config)
    store = StudioStore(get_async_session_factory(), config)
    worker_id = f"vercel:{uuid4()}"
    async with store.factory() as session:
        repo = JobRepository(session)
        job = await repo.claim_next_async(
            worker_id=worker_id,
            lease_seconds=90,
            run_id=run_id,
            job_types=("studio.advance", "studio.cleanup"),
        )
        if job is None:
            # A concurrent delivery may still own the SQL lease. Redelivery recovers
            # a process killed before its claim was released.
            run = await store.get_run(run_id)
            return run.status in WORKING
        job_id, generation, job_type = job.id, job.claim_generation, job.job_type
        await session.commit()
    context = current_job_claim.set(JobClaim(job_id, worker_id, generation))
    terminal = job_type == "studio.cleanup"
    try:
        async with asyncio.timeout(65):
            if not terminal:
                # Recorded media is already available. Keep its short internal
                # graph waits in this invocation, rather than requiring a new
                # platform delivery for every synthetic provider callback.
                # The native graph and SQL remain the owners of all transitions.
                for _ in range(4):
                    terminal = await advance_monthly_workflow(run_id)
                    current = await store.get_run(run_id)
                    if terminal or current.status not in WORKING:
                        break
    except StudioError as exc:
        await store.stop(run_id, exc.code)
        terminal = True
    finally:
        current_job_claim.reset(context)
    async with store.factory() as session:
        repo = JobRepository(session)
        try:
            if terminal:
                await repo.mark_succeeded_async(
                    job_id, worker_id=worker_id, claim_generation=generation
                )
            else:
                await repo.mark_failed_async(
                    job_id,
                    worker_id=worker_id,
                    claim_generation=generation,
                    error="studio_waiting",
                    retry_delay_seconds=0,
                )
            await session.commit()
        except StaleJobClaim:
            await session.rollback()
            return True
    run = await store.get_run(run_id)
    logger.info(
        "recorded_queue_progress",
        run_id=str(run_id),
        status=run.status,
        claim_generation=generation,
    )
    if terminal:
        # Complete any terminal cleanup receipt, never delete shared sample objects.
        async with store.factory() as session:
            pending = await session.scalar(
                select(Job.id).where(
                    Job.run_id == run_id, Job.job_type == "studio.cleanup", Job.status == "pending"
                )
            )
        return pending is not None
    return run.status in WORKING


@subscribe(topic=TOPIC, max_concurrency=2, max_attempts=30, retry_after=15)
async def advance_recorded(message: Message[dict[str, str]]) -> None:
    if await process_run(UUID(message.payload["run_id"])):
        raise RetryAfter(2)
