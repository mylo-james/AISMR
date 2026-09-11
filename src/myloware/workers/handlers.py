"""Job handlers for the Postgres-backed worker queue."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from uuid import UUID

from llama_stack_client import LlamaStackClient

from myloware.config import settings
from myloware.config.provider_modes import effective_remotion_provider, effective_sora_provider
from myloware.observability.logging import get_logger
from myloware.services.openai_videos import (
    download_openai_video_content_to_tempfile,
    retrieve_openai_video_job,
)
from myloware.services.remotion_urls import normalize_remotion_output_url
from myloware.services.render_local import LocalRemotionProvider
from myloware.services.transcode import transcode_video
from myloware.storage.models import ArtifactType, RunStatus
from myloware.storage.repositories import ArtifactRepository, JobRepository, RunRepository
from myloware.workers.exceptions import JobReschedule
from myloware.workers.job_types import (
    JOB_LANGGRAPH_HITL_RESUME,
    JOB_LANGGRAPH_RESUME,
    JOB_LANGGRAPH_RESUME_RENDER,
    JOB_LANGGRAPH_RESUME_VIDEOS,
    JOB_REMOTION_POLL,
    JOB_RUN_EXECUTE,
    JOB_SORA_POLL,
    JOB_WEBHOOK_REMOTION,
    JOB_WEBHOOK_SORA,
    idempotency_resume_render,
    idempotency_resume_videos,
)
from myloware.workflows.langgraph.resume import resume_after_render, resume_after_videos
from myloware.workflows.langgraph.workflow import run_workflow_async

logger = get_logger(__name__)


def _as_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _safe_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        if isinstance(value, str):
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
    return {}


def _extract_render_job_id(artifacts: list[Any]) -> str | None:
    """Return latest render_job_id from editor_output artifacts."""
    for artifact in reversed(artifacts):
        if getattr(artifact, "artifact_type", None) != ArtifactType.EDITOR_OUTPUT.value:
            continue
        meta = getattr(artifact, "artifact_metadata", None) or {}
        if not isinstance(meta, dict):
            continue
        job_id = meta.get("render_job_id") or meta.get("job_id")
        if isinstance(job_id, str) and job_id:
            return job_id
    return None


async def handle_job(
    *,
    job_type: str,
    run_id: UUID | None,
    payload: Dict[str, Any],
    session_run_repo: RunRepository,
    session_artifact_repo: ArtifactRepository,
    session_job_repo: JobRepository,
    llama_client: LlamaStackClient,
) -> None:
    """Execute the job payload and persist any outputs.

    This function should be called inside a short-lived DB session.
    """
    if job_type == "studio.cleanup":
        if run_id is None:
            raise ValueError("studio.cleanup requires run_id")
        from sqlalchemy import select

        from myloware.config.studio import get_studio_settings
        from myloware.storage.database import get_async_session_factory
        from myloware.storage.studio_models import StudioGalleryProjectionIntent
        from myloware.storage.studio_store import StudioError, StudioStore
        from myloware.studio.library_service import StudioLibraryService
        from myloware.studio.portfolio_runtime import open_portfolio_library

        await session_job_repo.session.rollback()
        store = StudioStore(get_async_session_factory(), get_studio_settings())
        # Cross-mode publication is driven solely by source intents committed
        # with explicit consent.  It completes before any source retention can
        # remove its final, and remains retry-safe after either database restarts.
        try:
            async with open_portfolio_library(store) as portfolio:
                await portfolio.initialize()
                async with store.factory() as source_session:
                    intents = (
                        await source_session.scalars(
                            select(StudioGalleryProjectionIntent).where(
                                StudioGalleryProjectionIntent.run_id == run_id,
                                StudioGalleryProjectionIntent.state.in_(
                                    ("pending", "projecting", "complete")
                                ),
                            )
                        )
                    ).all()
                for intent in intents:
                    if intent.state != "complete":
                        await portfolio.project_source_intent(
                            intent.id,
                            source_final=store.config.media_root / str(run_id) / "final.mp4",
                        )
                    # A persisted acknowledgement proves activation succeeded.
                    # Revisit it after a crash between acknowledgement and
                    # source cleanup, even if that library entry later retired.
                    from myloware.workers.claims import require_current_claim

                    async with store.factory() as source_session, source_session.begin():
                        await require_current_claim(source_session)
                    await asyncio.to_thread(
                        StudioLibraryService(store).files.remove_projected_source, run_id
                    )
                    await StudioLibraryService(store)._renderer_copy(await store.get_run(run_id))
                await portfolio.reconcile()
        except StudioError as exc:
            if exc.code not in {"library_database_missing", "library_media_root_missing"}:
                raise
        # Cleanup has its own durable retry budget. A cleanup failure must never
        # change an accepted video's state or construct paid provider clients.
        await StudioLibraryService(store).cleanup(run_id)
        return

    if job_type == "studio.advance":
        if run_id is None:
            raise ValueError("studio.advance requires run_id")
        from myloware.config.studio import get_studio_settings
        from myloware.storage.studio_store import StudioError
        from myloware.studio.service import build_studio_service, open_studio_service
        from myloware.workflows.langgraph.studio import advance_monthly_workflow

        # Release the worker's read transaction before stage owners commit their
        # short transactions. Provider calls never hold the queue row lock.
        await session_job_repo.session.rollback()
        try:
            done = await advance_monthly_workflow(run_id)
        except StudioError as exc:
            # The recovery store must share the live composition root's cleanup.
            # Do not leave an OpenAI or provider client open after a failed stage.
            async with open_studio_service(build_studio_service) as service:
                await service.store.stop(run_id, exc.code)
            return
        if not done:
            raise JobReschedule(
                reason="studio_waiting", retry_delay_seconds=get_studio_settings().poll_seconds
            )
        return

    if job_type == JOB_RUN_EXECUTE:
        if run_id is None:
            raise ValueError("run.execute requires run_id")
        run = await session_run_repo.get_async(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found")
        # Idempotency: only start from PENDING. If already started, do nothing.
        if run.status != RunStatus.PENDING.value:
            logger.info("run_execute_skipped", run_id=str(run_id), status=run.status)
            return
        vector_db_id = getattr(run, "vector_db_id", None) or payload.get("vector_db_id")
        if not vector_db_id:
            raise ValueError("vector_db_id missing for run.execute")
        await run_workflow_async(client=llama_client, run_id=run_id, vector_db_id=str(vector_db_id))
        return

    if job_type == JOB_SORA_POLL:
        if run_id is None:
            raise ValueError("sora.poll requires run_id")

        # Poll only in real mode; fake/off providers should rely on webhooks or fixtures.
        if effective_sora_provider(settings) != "real":
            logger.info("sora_poll_skipped_non_real", run_id=str(run_id))
            return

        run = await session_run_repo.get_async(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found")

        # Stop polling once the run is no longer waiting for Sora clips.
        if run.status in {
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.REJECTED.value,
        }:
            logger.info("sora_poll_terminal_run", run_id=str(run_id), status=str(run.status))
            return
        if run.status != RunStatus.AWAITING_VIDEO_GENERATION.value:
            logger.info("sora_poll_not_waiting", run_id=str(run_id), status=str(run.status))
            return

        artifacts = await session_artifact_repo.get_by_run_async(run_id)

        # Determine task list (prefer explicit run artifact).
        pending_task_ids = []
        artifacts_dict = getattr(run, "artifacts", None) or {}
        raw_pending = artifacts_dict.get("pending_task_ids")
        if isinstance(raw_pending, list):
            pending_task_ids = [str(t) for t in raw_pending if t]

        task_mapping: dict[str, dict[str, Any]] = {}
        expected_count = 1
        manifests = [
            a
            for a in artifacts
            if a.artifact_type == ArtifactType.CLIP_MANIFEST.value
            and a.artifact_metadata
            and a.artifact_metadata.get("type") == "task_metadata_mapping"
            and a.content
        ]
        if manifests:
            latest_manifest = manifests[-1]
            meta = latest_manifest.artifact_metadata or {}
            try:
                expected_count = int(meta.get("task_count") or expected_count)
            except Exception:
                expected_count = expected_count
            try:
                parsed = json.loads(latest_manifest.content or "{}")
            except Exception:
                parsed = {}
            if isinstance(parsed, dict):
                task_mapping = {
                    str(k): (v if isinstance(v, dict) else {}) for k, v in parsed.items() if k
                }
                if not pending_task_ids:
                    pending_task_ids = list(task_mapping.keys())

        if not pending_task_ids:
            raise ValueError("sora.poll could not determine pending task ids for run")

        # Idempotency: skip tasks we've already stored as clips.
        existing_task_ids: set[str] = set()
        for art in artifacts:
            if art.artifact_type != ArtifactType.VIDEO_CLIP.value:
                continue
            raw_meta = art.artifact_metadata
            artifact_meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
            task_id = artifact_meta.get("task_id")
            if isinstance(task_id, str) and task_id:
                existing_task_ids.add(task_id)

        def _video_index_for(task_id: str) -> int:
            raw = (task_mapping.get(task_id) or {}).get("video_index") or (
                task_mapping.get(task_id) or {}
            ).get("videoIndex")
            try:
                return int(raw)
            except Exception:
                return 0

        task_statuses: dict[str, dict[str, Any]] = {}
        completed_now = 0
        failed_now: str | None = None

        for task_id in pending_task_ids:
            if task_id in existing_task_ids:
                task_statuses[task_id] = {"status": "completed", "progress": 100}
                continue

            job = await retrieve_openai_video_job(task_id)
            status_value = str(job.get("status") or "").strip()
            progress_raw = job.get("progress")
            progress = None
            try:
                progress = int(progress_raw) if progress_raw is not None else None
            except Exception:
                progress = None

            task_statuses[task_id] = {
                "status": status_value or "unknown",
                "progress": progress,
            }

            if status_value == "failed":
                error_obj = job.get("error")
                message = None
                if isinstance(error_obj, dict):
                    message = error_obj.get("message")
                failed_now = str(message or "OpenAI video job failed")
                break

            if status_value != "completed":
                continue

            downloaded_video_path: Path | None = None
            try:
                downloaded_video_path = await download_openai_video_content_to_tempfile(task_id)
                original_video_url = downloaded_video_path.as_uri()
                video_index = _video_index_for(task_id)
                transcoded_url = await transcode_video(original_video_url, run_id, video_index)
            finally:
                if downloaded_video_path is not None:
                    try:
                        downloaded_video_path.unlink(missing_ok=True)
                    except Exception:
                        logger.debug(
                            "openai_video_download_cleanup_failed",
                            run_id=str(run_id),
                            task_id=task_id,
                        )

            if not transcoded_url:
                failed_now = "Transcode failed (ffmpeg missing or codec error)"
                break

            await session_artifact_repo.create_async(
                run_id=run_id,
                persona="producer",
                artifact_type=ArtifactType.VIDEO_CLIP,
                uri=transcoded_url,
                metadata={
                    "task_id": task_id,
                    "video_index": _video_index_for(task_id),
                    "source": "sora_poll",
                },
            )
            existing_task_ids.add(task_id)
            completed_now += 1

        # Persist polling telemetry for UI/debugging.
        clip_count = (
            sum(
                1
                for art in artifacts
                if art.artifact_type == ArtifactType.VIDEO_CLIP.value and getattr(art, "uri", None)
            )
            + completed_now
        )
        overall_progress = None
        try:
            pct_values: list[int] = []
            for task_id in pending_task_ids:
                entry = task_statuses.get(task_id) or {}
                status_val = entry.get("status")
                if status_val == "completed" or task_id in existing_task_ids:
                    pct_values.append(100)
                    continue
                progress_val = entry.get("progress")
                if isinstance(progress_val, int):
                    pct_values.append(max(0, min(100, progress_val)))
                else:
                    pct_values.append(0)
            if pct_values:
                overall_progress = int(sum(pct_values) / len(pct_values))
        except Exception:
            overall_progress = None

        await session_run_repo.add_artifact_async(
            run_id,
            "sora_progress",
            {
                "expected": expected_count,
                "ready": clip_count,
                "progress_percent": overall_progress,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        if failed_now:
            await session_run_repo.update_async(
                run_id,
                status=RunStatus.FAILED.value,
                error=str(failed_now),
            )
            await session_run_repo.session.commit()
            return

        if clip_count >= expected_count:
            if run.status == RunStatus.AWAITING_VIDEO_GENERATION.value:
                await session_run_repo.update_async(run_id, status=RunStatus.AWAITING_RENDER.value)

            if not settings.disable_background_workflows:
                try:
                    await session_job_repo.enqueue_async(
                        JOB_LANGGRAPH_RESUME_VIDEOS,
                        run_id=run_id,
                        payload={},
                        idempotency_key=idempotency_resume_videos(run_id),
                        max_attempts=settings.job_max_attempts,
                    )
                except ValueError:
                    pass

            await session_run_repo.session.commit()
            logger.info(
                "sora_poll_complete",
                run_id=str(run_id),
                expected=expected_count,
                clip_count=clip_count,
            )
            return

        await session_run_repo.session.commit()
        # Not ready yet: reschedule with a gentle polling delay.
        raise JobReschedule(
            retry_delay_seconds=float(settings.job_poll_interval_seconds) + 10.0,
            reason=(f"Waiting for OpenAI video jobs ({clip_count}/{expected_count} clips ready)"),
        )

    if job_type == JOB_REMOTION_POLL:
        if run_id is None:
            raise ValueError("remotion.poll requires run_id")

        # Poll only in real mode; fake/off providers should rely on webhooks or fixtures.
        if effective_remotion_provider(settings) != "real":
            logger.info("remotion_poll_skipped_non_real", run_id=str(run_id))
            return

        run = await session_run_repo.get_async(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found")

        # Stop polling once the run is no longer waiting for a render.
        if run.status in {
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.REJECTED.value,
        }:
            logger.info("remotion_poll_terminal_run", run_id=str(run_id), status=str(run.status))
            return
        if run.status != RunStatus.AWAITING_RENDER.value:
            logger.info("remotion_poll_not_waiting", run_id=str(run_id), status=str(run.status))
            return

        service_url = str(getattr(settings, "remotion_service_url", "") or "").rstrip("/")
        if not service_url:
            raise ValueError("REMOTION_SERVICE_URL is required for remotion.poll")

        artifacts = await session_artifact_repo.get_by_run_async(run_id)
        render_job_id = _extract_render_job_id(artifacts)
        if not render_job_id:
            raise ValueError("remotion.poll could not determine render_job_id for run")

        provider = LocalRemotionProvider(service_url, timeout=10.0)
        render_job = await provider.get_status(render_job_id)

        status_value = str(getattr(render_job.status, "value", render_job.status) or "").lower()
        output_url = getattr(render_job, "artifact_url", None)
        error = getattr(render_job, "error", None)
        meta = getattr(render_job, "metadata", None) or {}
        raw_progress = meta.get("progress") if isinstance(meta, dict) else None
        progress_percent: int | None = None
        if raw_progress is not None:
            try:
                progress_percent = int(max(0.0, min(1.0, float(raw_progress))) * 100)
            except Exception:
                progress_percent = None

        # Persist polling telemetry for UI/debugging.
        await session_run_repo.add_artifact_async(
            run_id,
            "remotion_progress",
            {
                "job_id": render_job_id,
                "status": status_value or None,
                "progress_percent": progress_percent,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Service/transport failures: retry without failing the run.
        if error and (
            str(error).startswith("Remotion service unavailable")
            or str(error).startswith("Remotion service error")
            or str(error).startswith("Job not found")
        ):
            await session_run_repo.session.commit()
            raise JobReschedule(
                retry_delay_seconds=max(15.0, float(settings.job_poll_interval_seconds) + 10.0),
                reason=str(error),
            )

        if status_value in ("done", "completed", "ready") and output_url:
            normalized = normalize_remotion_output_url(str(output_url))

            # Idempotency: if we already stored this job_id, skip.
            for art in artifacts:
                if (
                    art.artifact_type == ArtifactType.RENDERED_VIDEO.value
                    and art.artifact_metadata
                    and art.artifact_metadata.get("render_job_id") == render_job_id
                ):
                    logger.info(
                        "remotion_poll_already_processed",
                        run_id=str(run_id),
                        job_id=str(render_job_id),
                    )
                    return

            await session_artifact_repo.create_async(
                run_id=run_id,
                persona="editor",
                artifact_type=ArtifactType.RENDERED_VIDEO,
                uri=str(normalized),
                metadata={
                    "render_job_id": render_job_id,
                    "job_id": render_job_id,
                    "status": status_value,
                    "source": "remotion_poll",
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            if not settings.disable_background_workflows:
                try:
                    await session_job_repo.enqueue_async(
                        JOB_LANGGRAPH_RESUME_RENDER,
                        run_id=run_id,
                        payload={"video_url": str(normalized)},
                        idempotency_key=idempotency_resume_render(run_id),
                        max_attempts=settings.job_max_attempts,
                    )
                except ValueError:
                    pass

            await session_run_repo.session.commit()
            logger.info(
                "remotion_poll_complete",
                run_id=str(run_id),
                job_id=str(render_job_id),
            )
            return

        if status_value in ("failed", "error") or error:
            await session_run_repo.update_async(
                run_id,
                status=RunStatus.FAILED.value,
                error=str(error or "Render failed"),
            )
            await session_run_repo.session.commit()
            logger.info(
                "remotion_poll_failed",
                run_id=str(run_id),
                job_id=str(render_job_id),
                error=str(error or ""),
            )
            return

        await session_run_repo.session.commit()
        raise JobReschedule(
            retry_delay_seconds=max(10.0, float(settings.job_poll_interval_seconds) + 10.0),
            reason=(
                f"Waiting for Remotion render (status={status_value or 'unknown'}"
                + (f", progress={progress_percent}%" if isinstance(progress_percent, int) else "")
                + ")"
            ),
        )

    if job_type == JOB_LANGGRAPH_RESUME_VIDEOS:
        if run_id is None:
            raise ValueError("resume_after_videos requires run_id")
        await resume_after_videos(run_id)
        return

    if job_type == JOB_LANGGRAPH_RESUME_RENDER:
        if run_id is None:
            raise ValueError("resume_after_render requires run_id")
        video_url = str(payload.get("video_url") or "")
        if not video_url:
            raise ValueError("resume_after_render requires video_url")
        await resume_after_render(run_id, video_url)
        return

    if job_type == JOB_LANGGRAPH_HITL_RESUME:
        if run_id is None:
            raise ValueError("langgraph.hitl_resume requires run_id")

        gate = str(payload.get("gate") or "")
        if gate not in {"ideation", "publish"}:
            raise ValueError("langgraph.hitl_resume requires gate=ideation|publish")

        approved = payload.get("approved")
        if not isinstance(approved, bool):
            raise ValueError("langgraph.hitl_resume requires boolean approved")

        comment = payload.get("comment")
        comment_str = str(comment) if comment is not None else None

        run = await session_run_repo.get_async(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found")

        expected_status = (
            RunStatus.AWAITING_IDEATION_APPROVAL.value
            if gate == "ideation"
            else RunStatus.AWAITING_PUBLISH_APPROVAL.value
        )
        if run.status != expected_status:
            logger.info(
                "langgraph_hitl_resume_skipped",
                run_id=str(run_id),
                gate=gate,
                approved=approved,
                status=str(run.status),
            )
            return

        from myloware.workflows.langgraph.hitl import resume_hitl_gate

        await resume_hitl_gate(
            run_id,
            gate=gate,  # type: ignore[arg-type]
            approved=approved,
            comment=comment_str,
        )
        return

    if job_type == JOB_LANGGRAPH_RESUME:
        if run_id is None:
            raise ValueError("langgraph.resume requires run_id")
        if not settings.use_langgraph_engine:
            raise ValueError("LangGraph engine is not enabled")

        from langgraph.types import Command

        from myloware.workflows.langgraph.graph import ensure_checkpointer_initialized, get_graph

        if not settings.database_url.startswith("sqlite"):
            await ensure_checkpointer_initialized()

        graph = get_graph()
        config = {"configurable": {"thread_id": str(run_id)}}

        resume_data = _safe_json(payload.get("resume_data"))
        interrupt_id = payload.get("interrupt_id")

        graph_state = await graph.aget_state(config)
        interrupts = getattr(graph_state, "interrupts", None) or []

        if not interrupts:
            logger.info("langgraph_resume_no_interrupts", run_id=str(run_id))
            return

        if not interrupt_id:
            first_interrupt = interrupts[0]
            interrupt_id = getattr(first_interrupt, "id", None) or getattr(
                first_interrupt, "interrupt_id", None
            )

        resume_argument = {interrupt_id: resume_data} if interrupt_id else resume_data
        await graph.ainvoke(Command(resume=resume_argument), config=config, durability="sync")
        logger.info("langgraph_resumed", run_id=str(run_id), interrupt_id=str(interrupt_id or ""))
        return

    if job_type == JOB_WEBHOOK_SORA:
        if run_id is None:
            raise ValueError("webhook.sora requires run_id")

        # Mirror the API handler behavior, but in the worker.
        code_raw = payload.get("code")
        try:
            code = int(code_raw) if code_raw is not None else None
        except Exception:
            code = None
        state = payload.get("state")
        status_msg = str(payload.get("status_msg") or "")
        task_id = payload.get("task_id")
        event_type_raw = payload.get("event_type")
        event_type = str(event_type_raw) if isinstance(event_type_raw, str) else None
        video_index = int(payload.get("video_index") or 0)
        video_urls = payload.get("video_urls") or []
        if isinstance(video_urls, str):
            try:
                video_urls = json.loads(video_urls)
            except Exception:
                video_urls = [video_urls] if video_urls else []
        if not isinstance(video_urls, list):
            video_urls = []

        is_failure = (
            (code is not None and code != 200) or state == "fail" or event_type == "video.failed"
        )
        is_completion = bool(video_urls) or event_type == "video.completed"

        # Failure callback (do not raise; mark run failed and stop)
        if is_failure:
            error_msg = status_msg or (event_type or "Video generation failed")
            await session_run_repo.update_async(
                run_id, status=RunStatus.FAILED.value, error=f"OpenAI Sora error: {error_msg}"
            )
            return

        # Progress callback (not completion yet)
        if not is_completion:
            logger.info("sora_progress_ignored", run_id=str(run_id), task_id=task_id)
            return

        # Idempotency: if we already stored this task_id, skip.
        if task_id:
            existing = await session_artifact_repo.get_by_run_async(run_id)
            for art in existing:
                if (
                    art.artifact_type == ArtifactType.VIDEO_CLIP.value
                    and art.artifact_metadata
                    and art.artifact_metadata.get("task_id") == task_id
                ):
                    logger.info(
                        "sora_webhook_already_processed", run_id=str(run_id), task_id=task_id
                    )
                    return

        original_url: str | None = str(video_urls[0]) if video_urls else None
        downloaded_path: Path | None = None

        try:
            if not original_url and event_type == "video.completed":
                sora_mode = effective_sora_provider(settings)
                if sora_mode == "fake":
                    base = str(getattr(settings, "webhook_base_url", "") or "").rstrip("/")
                    if base and task_id:
                        original_url = f"{base}/v1/media/sora/{task_id}.mp4"
                else:
                    downloaded_path = await download_openai_video_content_to_tempfile(
                        str(task_id or "")
                    )
                    original_url = downloaded_path.as_uri()

            if not original_url:
                await session_run_repo.update_async(
                    run_id,
                    status=RunStatus.FAILED.value,
                    error="Sora webhook missing video URL and no downloadable content available",
                )
                return

            transcoded_url = await transcode_video(original_url, run_id, video_index)
        finally:
            if downloaded_path is not None:
                try:
                    downloaded_path.unlink(missing_ok=True)
                except Exception:
                    logger.debug(
                        "openai_video_download_cleanup_failed",
                        run_id=str(run_id),
                        task_id=str(task_id or ""),
                    )

        if not transcoded_url:
            # Stop: do not retry webhooks indefinitely; mark run failed clearly.
            await session_run_repo.update_async(
                run_id,
                status=RunStatus.FAILED.value,
                error="Transcode failed (ffmpeg missing or codec error)",
            )
            return

        metadata = _safe_json(payload.get("metadata"))
        artifact_metadata: Dict[str, Any] = {
            "task_id": task_id,
            "video_index": video_index,
            "source": "sora",
        }
        for key in ("topic", "sign", "object_name"):
            if key in metadata:
                artifact_metadata[key] = metadata[key]

        await session_artifact_repo.create_async(
            run_id=run_id,
            persona="producer",
            artifact_type=ArtifactType.VIDEO_CLIP,
            uri=transcoded_url,
            metadata=artifact_metadata,
        )

        # Update run projection and enqueue resume when all clips are ready.
        run = await session_run_repo.get_for_update_async(run_id)
        if not run:
            raise ValueError("Run not found")

        # Count clips + expected count based on CLIP_MANIFEST task_count metadata (default 1).
        artifacts = await session_artifact_repo.get_by_run_async(run_id)
        clip_count = sum(1 for a in artifacts if a.artifact_type == ArtifactType.VIDEO_CLIP.value)
        manifests = [
            a
            for a in artifacts
            if a.artifact_type == ArtifactType.CLIP_MANIFEST.value
            and a.artifact_metadata
            and a.artifact_metadata.get("type") == "task_metadata_mapping"
        ]
        manifest = manifests[-1] if manifests else None
        expected = 1
        if manifest and manifest.artifact_metadata:
            expected = int(manifest.artifact_metadata.get("task_count", 1) or 1)

        if clip_count >= expected:
            if run.status == RunStatus.FAILED.value:
                logger.info("run_failed_skip_resume_after_videos", run_id=str(run_id))
                return

            # Best-effort projection update for legacy consumers.
            if run.status == RunStatus.AWAITING_VIDEO_GENERATION.value:
                await session_run_repo.update_async(run_id, status=RunStatus.AWAITING_RENDER.value)

            if not settings.disable_background_workflows:
                try:
                    await session_job_repo.enqueue_async(
                        JOB_LANGGRAPH_RESUME_VIDEOS,
                        run_id=run_id,
                        payload={},
                        idempotency_key=idempotency_resume_videos(run_id),
                        max_attempts=settings.job_max_attempts,
                    )
                except ValueError:
                    pass
        return

    if job_type == JOB_WEBHOOK_REMOTION:
        if run_id is None:
            raise ValueError("webhook.remotion requires run_id")

        status_value = payload.get("status")
        error = payload.get("error")
        job_id = payload.get("job_id") or payload.get("id")
        output_url = payload.get("output_url") or payload.get("outputUrl")

        if status_value == "error" or error:
            await session_run_repo.update_async(
                run_id, status=RunStatus.FAILED.value, error=str(error or "Render failed")
            )
            return

        if status_value not in ("done", "completed", "ready"):
            logger.info(
                "remotion_progress_ignored",
                run_id=str(run_id),
                job_id=str(job_id or ""),
                status=str(status_value or ""),
            )
            return

        if not output_url:
            raise ValueError("No output_url in payload")

        output_url = normalize_remotion_output_url(str(output_url))

        # Idempotency: if we already stored this job_id, skip.
        if job_id:
            existing = await session_artifact_repo.get_by_run_async(run_id)
            for art in existing:
                if (
                    art.artifact_type == ArtifactType.RENDERED_VIDEO.value
                    and art.artifact_metadata
                    and art.artifact_metadata.get("render_job_id") == job_id
                ):
                    logger.info(
                        "remotion_webhook_already_processed",
                        run_id=str(run_id),
                        job_id=str(job_id),
                    )
                    return

        await session_artifact_repo.create_async(
            run_id=run_id,
            persona="editor",
            artifact_type=ArtifactType.RENDERED_VIDEO,
            uri=str(output_url),
            metadata={
                "render_job_id": job_id,
                "job_id": job_id,
                "status": status_value,
                "source": "remotion",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        if not settings.disable_background_workflows:
            try:
                await session_job_repo.enqueue_async(
                    JOB_LANGGRAPH_RESUME_RENDER,
                    run_id=run_id,
                    payload={"video_url": str(output_url)},
                    idempotency_key=idempotency_resume_render(run_id),
                    max_attempts=settings.job_max_attempts,
                )
            except ValueError:
                pass
        return

    raise ValueError(f"Unknown job_type: {job_type}")
