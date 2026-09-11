"""LangGraph resume helpers shared by API handlers and CLI ops.

These functions encapsulate the "resume from interrupt" behavior used by
webhook handlers and operator tooling. Keeping this outside FastAPI route
modules avoids cross-layer imports and keeps recovery paths testable.
"""

from __future__ import annotations

import time
from uuid import UUID

import anyio
from langgraph.types import Command

from myloware.config.settings import settings
from myloware.config.provider_modes import effective_sora_provider
from myloware.observability.logging import get_logger
from myloware.storage.database import get_async_session_factory
from myloware.storage.models import RunStatus
from myloware.storage.repositories import ArtifactRepository, RunRepository
from myloware.workflows.langgraph.graph import ensure_checkpointer_initialized, get_graph
from myloware.workflows.langgraph.utils import select_latest_video_clip_urls
from myloware.workflows.admission import StudioAdmissionRequiredError

logger = get_logger(__name__)


class ResumeRetryableError(RuntimeError):
    """Transient resume error that should be retried by background workers."""


async def _require_legacy_monthly_resume_allowed(run_id: UUID) -> None:
    """Reject legacy graph resumes for monthly rows before graph activity."""
    SessionLocal = get_async_session_factory()
    async with SessionLocal() as session:
        run = await RunRepository(session).get_async(run_id)
    from myloware.workflows.admission import require_legacy_monthly_engine_allowed

    require_legacy_monthly_engine_allowed(run.workflow_name if run else None)


async def resume_after_videos(
    run_id: UUID,
    *,
    raise_on_error: bool = False,
    fail_run_on_error: bool = True,
) -> None:
    """Resume a workflow after Sora clips are ready (wait_for_videos interrupt)."""
    logger.info("Resuming LangGraph workflow after videos: %s", run_id)

    try:
        await _require_legacy_monthly_resume_allowed(run_id)
        if not settings.database_url.startswith("sqlite"):
            await ensure_checkpointer_initialized()

        graph = get_graph()
        config = {"configurable": {"thread_id": str(run_id)}}

        SessionLocal = get_async_session_factory()
        async with SessionLocal() as session:
            artifact_repo = ArtifactRepository(session)
            artifacts = await artifact_repo.get_by_run_async(run_id)

        video_clips = select_latest_video_clip_urls(artifacts)
        if not video_clips:
            raise ValueError(f"No video clips found for run {run_id}")

        # Target the specific interrupt id so LangGraph applies payload correctly.
        resume_data = {"video_urls": video_clips}
        interrupt_id = None
        start = time.monotonic()
        # Fake Sora can deliver webhooks immediately (inside the producer agent turn),
        # before LangGraph reaches the wait_for_videos interrupt. Allow extra time so
        # we don't fail runs due to this dev-only timing race.
        max_wait_s = 30.0 if effective_sora_provider(settings) != "real" else 5.0
        poll_interval_s = 0.2

        while (time.monotonic() - start) < max_wait_s:
            graph_state = await graph.aget_state(config)
            interrupts = getattr(graph_state, "interrupts", None) or []
            for intr in interrupts:
                intr_val = getattr(intr, "value", None) or {}
                if isinstance(intr_val, dict) and intr_val.get("waiting_for") == "sora_webhook":
                    interrupt_id = getattr(intr, "id", None) or getattr(intr, "interrupt_id", None)
                    break
            if interrupt_id:
                break

            values = dict(getattr(graph_state, "values", {}) or {})
            current_step = values.get("current_step")
            status_val = values.get("status")
            if status_val in {
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.REJECTED.value,
            }:
                logger.info(
                    "Run %s is terminal in LangGraph (status=%s); skipping Sora resume",
                    run_id,
                    status_val,
                )
                return
            if current_step and current_step not in ("production", "wait_for_videos"):
                logger.info(
                    "Run %s is already past wait_for_videos (step=%s); skipping Sora resume",
                    run_id,
                    current_step,
                )
                return

            await anyio.sleep(poll_interval_s)

        if not interrupt_id:
            raise ResumeRetryableError(
                f"No sora_webhook interrupt found for run {run_id} after waiting {max_wait_s:.1f}s"
            )

        await graph.ainvoke(
            Command(resume={interrupt_id: resume_data}),
            config=config,
            durability="sync",
        )
        logger.info("LangGraph workflow resumed after videos: %s", run_id)

    except StudioAdmissionRequiredError:
        # Monthly visitor runs are owned by StudioStore. Do not mutate them from
        # this legacy recovery path after rejecting graph entry.
        raise
    except ResumeRetryableError as exc:
        logger.warning("Resume retryable: %s", exc)
        if raise_on_error or settings.workflow_dispatcher == "db":
            raise
        return
    except Exception as exc:
        logger.error("Failed to resume LangGraph workflow after videos: %s", exc, exc_info=True)
        if fail_run_on_error:
            SessionLocal = get_async_session_factory()
            async with SessionLocal() as session:
                run_repo = RunRepository(session)
                await run_repo.update_async(run_id, status=RunStatus.FAILED.value, error=str(exc))
                await session.commit()
        if raise_on_error:
            raise


async def resume_after_render(
    run_id: UUID,
    video_url: str,
    *,
    raise_on_error: bool = False,
    fail_run_on_error: bool = True,
) -> None:
    """Resume a workflow after Remotion render is complete (wait_for_render interrupt)."""
    logger.info("Resuming LangGraph workflow after render: %s", run_id)

    if not video_url:
        raise ValueError("video_url is required to resume after render")

    try:
        await _require_legacy_monthly_resume_allowed(run_id)
        if not settings.database_url.startswith("sqlite"):
            await ensure_checkpointer_initialized()

        graph = get_graph()
        config = {"configurable": {"thread_id": str(run_id)}}

        resume_data = {"video_url": video_url}
        interrupt_id = None
        start = time.monotonic()
        max_wait_s = 5.0
        poll_interval_s = 0.2

        while (time.monotonic() - start) < max_wait_s:
            graph_state = await graph.aget_state(config)
            interrupts = getattr(graph_state, "interrupts", None) or []
            for intr in interrupts:
                intr_val = getattr(intr, "value", None) or {}
                if isinstance(intr_val, dict) and intr_val.get("waiting_for") == "remotion_webhook":
                    interrupt_id = getattr(intr, "id", None) or getattr(intr, "interrupt_id", None)
                    break
            if interrupt_id:
                break

            values = dict(getattr(graph_state, "values", {}) or {})
            current_step = values.get("current_step")
            status_val = values.get("status")
            if status_val in {
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.REJECTED.value,
            }:
                logger.info(
                    "Run %s is terminal in LangGraph (status=%s); skipping Remotion resume",
                    run_id,
                    status_val,
                )
                return
            if current_step and current_step not in ("editing", "wait_for_render"):
                logger.info(
                    "Run %s is already past wait_for_render (step=%s); skipping Remotion resume",
                    run_id,
                    current_step,
                )
                return

            await anyio.sleep(poll_interval_s)

        if not interrupt_id:
            raise ResumeRetryableError(
                f"No remotion_webhook interrupt found for run {run_id} after waiting {max_wait_s:.1f}s"
            )

        await graph.ainvoke(
            Command(resume={interrupt_id: resume_data}),
            config=config,
            durability="sync",
        )
        logger.info("LangGraph workflow resumed after render: %s", run_id)

    except StudioAdmissionRequiredError:
        # Monthly visitor runs are owned by StudioStore. Do not mutate them from
        # this legacy recovery path after rejecting graph entry.
        raise
    except ResumeRetryableError as exc:
        logger.warning("Resume retryable: %s", exc)
        if raise_on_error or settings.workflow_dispatcher == "db":
            raise
        return
    except Exception as exc:
        logger.error("Failed to resume LangGraph workflow after render: %s", exc, exc_info=True)
        if fail_run_on_error:
            SessionLocal = get_async_session_factory()
            async with SessionLocal() as session:
                run_repo = RunRepository(session)
                await run_repo.update_async(run_id, status=RunStatus.FAILED.value, error=str(exc))
                await session.commit()
        if raise_on_error:
            raise
