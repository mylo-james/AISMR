"""Postgres-backed worker loop."""

from __future__ import annotations

import asyncio
import os
import socket
import time
from uuid import UUID

import anyio

from myloware.config import settings
from myloware.llama_clients import get_sync_client
from myloware.observability.logging import get_logger
from myloware.storage.database import get_async_session_factory
from myloware.storage.models import JobStatus
from myloware.storage.repositories import (
    ArtifactRepository,
    DeadLetterRepository,
    JobRepository,
    RunRepository,
    StaleJobClaim,
)
from myloware.workers.exceptions import JobReschedule
from myloware.workers.handlers import handle_job

logger = get_logger(__name__)
HOUSEKEEPING_INTERVAL_SECONDS = 24 * 60 * 60


def _default_worker_id() -> str:
    host = socket.gethostname()
    pid = os.getpid()
    return f"{host}:{pid}"


async def _lease_heartbeat(
    job_id: UUID,
    worker_id: str,
    claim_generation: int,
    lease_seconds: float,
    stop_event: anyio.Event,
) -> None:
    """Periodically extend the lease for a running job.

    Uses a separate DB session to avoid committing partial side effects from the
    job execution session.
    """
    interval_seconds = max(1.0, min(float(lease_seconds) / 3.0, 30.0))
    SessionLocal = get_async_session_factory()
    while True:
        with anyio.move_on_after(interval_seconds):
            await stop_event.wait()
        if stop_event.is_set():
            return
        try:
            async with SessionLocal() as hb_session:
                hb_repo = JobRepository(hb_session)
                await hb_repo.touch_lease_async(
                    job_id,
                    worker_id=worker_id,
                    claim_generation=claim_generation,
                    lease_seconds=float(lease_seconds),
                )
                await hb_session.commit()
        except StaleJobClaim:
            logger.info("job_lease_renew_stale", job_id=str(job_id))
            return
        except Exception:
            logger.warning("job_lease_renew_failed", job_id=str(job_id), exc_info=True)


async def _process_one_job(
    job_id: UUID, worker_id: str, claim_generation: int, *, lease_seconds: float
) -> None:
    """Execute one claimed job and mark succeeded/failed."""
    SessionLocal = get_async_session_factory()
    async with SessionLocal() as session:
        job_repo = JobRepository(session)
        run_repo = RunRepository(session)
        artifact_repo = ArtifactRepository(session)
        dlq_repo = DeadLetterRepository(session)

        job = await job_repo.get_current_claim_async(
            job_id, worker_id=worker_id, claim_generation=claim_generation
        )
        if job is None:
            return

        job_type = str(job.job_type)
        run_id = job.run_id
        payload = dict(job.payload or {})
        claimed_attempts = int(job.attempts or 0)
        claimed_max_attempts = int(job.max_attempts or 0)

        llama_client = get_sync_client()

        stop_event = anyio.Event()
        handler_exc: Exception | None = None
        from myloware.workers.claims import JobClaim, current_job_claim

        claim_context = current_job_claim.set(JobClaim(job_id, worker_id, claim_generation))
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                _lease_heartbeat,
                job_id,
                worker_id,
                claim_generation,
                float(lease_seconds),
                stop_event,
            )
            try:
                await handle_job(
                    job_type=job_type,
                    run_id=run_id,
                    payload=payload,
                    session_run_repo=run_repo,
                    session_artifact_repo=artifact_repo,
                    session_job_repo=job_repo,
                    llama_client=llama_client,
                )
            except Exception as exc:
                handler_exc = exc
            finally:
                current_job_claim.reset(claim_context)
                stop_event.set()

        if handler_exc is None:
            try:
                await job_repo.mark_succeeded_async(
                    job_id, worker_id=worker_id, claim_generation=claim_generation
                )
            except StaleJobClaim:
                await session.rollback()
                logger.info("job_completion_stale", job_id=str(job_id), job_type=job_type)
                return
            await session.commit()
            logger.info("job_succeeded", job_id=str(job_id), job_type=job_type)
            return

        # Rescheduled: treat as expected control flow (no stacktrace, no DLQ).
        if isinstance(handler_exc, JobReschedule):
            try:
                await session.rollback()
            except Exception:
                logger.warning("job_failure_rollback_failed", job_id=str(job_id), exc_info=True)

            try:
                status = await job_repo.mark_failed_async(
                    job_id,
                    worker_id=worker_id,
                    claim_generation=claim_generation,
                    error=str(handler_exc.reason),
                    retry_delay_seconds=float(handler_exc.retry_delay_seconds),
                )
            except StaleJobClaim:
                await session.rollback()
                logger.info("job_failure_stale", job_id=str(job_id), job_type=job_type)
                return
            await session.commit()

            if status == JobStatus.FAILED:
                logger.warning(
                    "job_reschedule_exhausted",
                    job_id=str(job_id),
                    job_type=job_type,
                    attempts=claimed_attempts,
                    max_attempts=claimed_max_attempts,
                    error=str(handler_exc.reason),
                )
            else:
                logger.info(
                    "job_rescheduled",
                    job_id=str(job_id),
                    job_type=job_type,
                    retry_delay_seconds=float(handler_exc.retry_delay_seconds),
                    reason=str(handler_exc.reason),
                )
            return

        # Failed: rollback any partial/failed transaction, then retry with backoff.
        try:
            await session.rollback()
        except Exception:
            logger.warning("job_failure_rollback_failed", job_id=str(job_id), exc_info=True)

        delay = float(settings.job_retry_delay_seconds) * max(1.0, float(claimed_attempts or 1))
        try:
            status = await job_repo.mark_failed_async(
                job_id,
                worker_id=worker_id,
                claim_generation=claim_generation,
                error=str(handler_exc),
                retry_delay_seconds=delay,
            )
        except StaleJobClaim:
            await session.rollback()
            logger.info("job_failure_stale", job_id=str(job_id), job_type=job_type)
            return
        if status == JobStatus.FAILED:
            if job_type.startswith("webhook.") and run_id is not None:
                try:
                    source = "sora" if job_type.endswith("sora") else "remotion"
                    await dlq_repo.create_async(
                        source=source,
                        run_id=run_id,
                        payload={"job_type": job_type, "payload": payload},
                        error=str(handler_exc),
                        attempts=claimed_attempts,
                    )
                except Exception:
                    logger.warning("dlq_write_failed", job_id=str(job_id), exc_info=True)
        await session.commit()
        logger.warning(
            "job_failed",
            job_id=str(job_id),
            job_type=job_type,
            attempts=claimed_attempts,
            max_attempts=claimed_max_attempts,
            error=str(handler_exc),
            exc_info=handler_exc,
        )


async def _run_studio_housekeeping() -> None:
    """Compose the daily Studio pass outside any claimed-job transaction."""
    from myloware.config.studio import get_studio_settings
    from myloware.storage.studio_store import StudioStore
    from myloware.studio.housekeeping import run_studio_housekeeping

    store = StudioStore(get_async_session_factory(), get_studio_settings())
    await run_studio_housekeeping(store)


async def run_worker(*, once: bool = False) -> None:
    """Run the worker event loop.

    Args:
        once: If true, process at most one job and exit (useful for tests/ops).
    """
    if settings.disable_background_workflows:
        raise RuntimeError("Worker cannot run with DISABLE_BACKGROUND_WORKFLOWS=true")

    # Explicit LangGraph engine lifecycle for this worker process.
    if settings.use_langgraph_engine:
        from myloware.workflows.langgraph.graph import LangGraphEngine, set_langgraph_engine

        engine = LangGraphEngine()
        set_langgraph_engine(engine)
        await engine.ensure_checkpointer_initialized()

    worker_id = settings.worker_id or _default_worker_id()
    concurrency = max(1, int(settings.worker_concurrency or 1))
    lease_seconds = float(settings.job_lease_seconds)
    poll_interval = float(settings.job_poll_interval_seconds)

    logger.info(
        "worker_start",
        worker_id=worker_id,
        concurrency=concurrency,
        lease_seconds=lease_seconds,
        poll_interval=poll_interval,
    )

    limiter = anyio.Semaphore(concurrency)
    next_housekeeping_at = time.monotonic()

    async def _maybe_run_housekeeping() -> None:
        nonlocal next_housekeeping_at
        if time.monotonic() < next_housekeeping_at:
            return
        # Advance before running: a failed pass is reported and retried at the
        # next daily boundary without a tight-loop against the database.
        next_housekeeping_at = time.monotonic() + HOUSEKEEPING_INTERVAL_SECONDS
        try:
            await _run_studio_housekeeping()
        except Exception:
            logger.warning("studio_housekeeping_failed", exc_info=True)

    async def _claim_job() -> tuple[UUID, int] | None:
        try:
            SessionLocal = get_async_session_factory()
            async with SessionLocal() as session:
                job_repo = JobRepository(session)
                job = await job_repo.claim_next_async(
                    worker_id=worker_id, lease_seconds=lease_seconds
                )
                await session.commit()
                return (job.id, int(job.claim_generation)) if job else None
        except Exception:
            logger.warning("job_claim_failed", exc_info=True)
            return None

    async def _run_claimed(jid: UUID, claim_generation: int) -> None:
        try:
            await _process_one_job(jid, worker_id, claim_generation, lease_seconds=lease_seconds)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.error("job_unhandled_exception", job_id=str(jid), exc_info=True)
            try:
                SessionLocal = get_async_session_factory()
                async with SessionLocal() as session:
                    job_repo = JobRepository(session)
                    delay = float(settings.job_retry_delay_seconds)
                    await job_repo.mark_failed_async(
                        jid,
                        worker_id=worker_id,
                        claim_generation=claim_generation,
                        error=str(exc),
                        retry_delay_seconds=delay,
                    )
                    await session.commit()
            except Exception:
                logger.error(
                    "job_unhandled_exception_mark_failed_failed",
                    job_id=str(jid),
                    exc_info=True,
                )
        finally:
            limiter.release()

    if once:
        claim = await _claim_job()
        if claim is None:
            return
        jid, claim_generation = claim
        await _process_one_job(jid, worker_id, claim_generation, lease_seconds=lease_seconds)
        return

    async with anyio.create_task_group() as tg:
        while True:
            await _maybe_run_housekeeping()
            await limiter.acquire()
            claim = await _claim_job()
            if claim is None:
                limiter.release()
                await anyio.sleep(poll_interval)
                continue
            jid, claim_generation = claim
            tg.start_soon(_run_claimed, jid, claim_generation)
