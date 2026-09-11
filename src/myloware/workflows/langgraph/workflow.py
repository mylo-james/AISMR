"""LangGraph-based workflow execution - single source of truth.

This module replaces orchestrator.py and provides LangGraph-based workflow execution.
All workflow execution now goes through LangGraph for consistency and replayability.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional
from uuid import UUID

from myloware.config import settings
from myloware.config.provider_modes import effective_remotion_provider
from langgraph.types import Command
from llama_stack_client import LlamaStackClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from myloware.observability.logging import get_logger
from myloware.storage.database import get_async_session_factory
from myloware.storage.models import ArtifactType, RunStatus
from myloware.storage.repositories import ArtifactRepository, RunRepository
from myloware.tools.remotion import RemotionRenderTool
from myloware.storage.object_store import resolve_s3_uri_async
from myloware.services.render_provider import RenderStatus, get_render_provider
from myloware.services.remotion_urls import normalize_remotion_output_url
from myloware.workflows.langgraph.graph import get_graph
from myloware.workflows.langgraph.state import VideoWorkflowState
from myloware.workflows.langgraph.utils import (
    normalize_transcoded_url,
    select_latest_video_clip_urls,
)
from myloware.workflows.state import WorkflowResult
from myloware.config.projects import load_project

logger = get_logger(__name__)

__all__ = [
    "run_workflow_async",
    "run_workflow",
    "continue_after_ideation",
    "continue_after_producer",
    "continue_after_render",
    "continue_after_publish_approval",
    "continue_after_publish",
    "fork_from_clips",
    "repair_sora_clips",
    "repair_render",
    "resume_run",
    "create_pending_run",
]


async def repair_sora_clips(
    run_id: UUID,
    *,
    video_indexes: list[int] | None = None,
    force: bool = False,
) -> WorkflowResult:
    """Re-submit only missing Sora clips for a run, reusing already-good clips.

    This is an operator-only cost guard: we keep existing VIDEO_CLIP artifacts and submit
    new OpenAI video tasks only for missing indices. Requires a stored SORA_REQUEST
    artifact from the original producer submission.
    """
    from myloware.config.provider_modes import effective_sora_provider
    from myloware.tools.sora import SoraGenerationTool

    provider = effective_sora_provider(settings)
    if provider != "real":
        raise ValueError("repair_sora_clips requires SORA_PROVIDER=real (paid workflow parity)")

    SessionLocal = get_async_session_factory()
    async with _session_ctx(SessionLocal) as session:
        run_repo = RunRepository(session)
        artifact_repo = ArtifactRepository(session)

        # Serialize repairs per run to avoid concurrent paid submissions.
        run = await run_repo.get_for_update_async(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")
        allowed_statuses = {RunStatus.FAILED.value, RunStatus.AWAITING_VIDEO_GENERATION.value}
        if run.status not in allowed_statuses:
            raise ValueError(
                "repair_sora_clips is only allowed when a run is awaiting video generation or failed "
                f"(run.status={run.status})"
            )

        artifacts = await artifact_repo.get_by_run_async(run_id)

        # Latest clip manifest is the active task set.
        manifests: list[Any] = []
        for a in artifacts:
            if getattr(a, "artifact_type", None) != ArtifactType.CLIP_MANIFEST.value:
                continue
            meta = getattr(a, "artifact_metadata", None)
            if not isinstance(meta, dict):
                continue
            if meta.get("type") != "task_metadata_mapping":
                continue
            if not getattr(a, "content", None):
                continue
            manifests.append(a)
        if not manifests:
            raise ValueError(f"No clip_manifest found for run {run_id}")
        active_manifest = manifests[-1]
        meta = active_manifest.artifact_metadata or {}
        expected_raw = meta.get("task_count")
        expected_count = int(expected_raw) if isinstance(expected_raw, int | str) else 1
        if expected_count <= 0:
            expected_count = 1

        # Select newest clip per video_index (artifacts are ordered by created_at asc).
        clips_by_index: dict[int, Any] = {}
        for art in artifacts:
            if getattr(art, "artifact_type", None) != ArtifactType.VIDEO_CLIP.value:
                continue
            uri = getattr(art, "uri", None)
            if not uri:
                continue
            art_meta = getattr(art, "artifact_metadata", None) or {}
            idx = None
            try:
                idx = int(
                    art_meta.get("video_index")
                    or art_meta.get("videoIndex")
                    or art_meta.get("idx")
                    or art_meta.get("index")
                )
            except (TypeError, ValueError):
                idx = None
            if idx is None:
                continue
            clips_by_index[idx] = art

        requested = None
        if video_indexes is not None:
            requested = sorted({int(i) for i in video_indexes})
        missing = [
            i
            for i in (requested if requested is not None else range(expected_count))
            if force or i not in clips_by_index
        ]
        if not missing:
            return WorkflowResult(
                run_id=str(run_id),
                status=run.status,
                current_step=run.current_step or "unknown",
                error="No missing clips to repair",
            )

        # Load Sora request payloads (latest-first) and resolve prompts for each missing index.
        request_artifacts = [
            a
            for a in artifacts
            if getattr(a, "artifact_type", None) == ArtifactType.SORA_REQUEST.value
            and getattr(a, "content", None)
        ]
        if not request_artifacts:
            raise ValueError(
                f"No stored SORA_REQUEST payloads for run {run_id}. "
                "Cannot safely resubmit only failed clips."
            )

        remaining = set(missing)
        resolved_specs: dict[int, dict[str, Any]] = {}
        submission_params: dict[str, Any] = {}

        for req_art in reversed(request_artifacts):
            payload = None
            try:
                payload = json.loads(req_art.content or "{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = None
            if not isinstance(payload, dict):
                continue
            videos = payload.get("videos")
            if isinstance(videos, list):
                for item in videos:
                    if not isinstance(item, dict):
                        continue
                    idx = None
                    try:
                        idx = int(item.get("video_index"))
                    except (TypeError, ValueError):
                        idx = None
                    if idx is None:
                        continue
                    if idx in remaining:
                        resolved_specs[idx] = dict(item)
                        remaining.discard(idx)
            if not submission_params:
                # Keep the first (newest) payload params we find.
                submission_params = {
                    "aspect_ratio": payload.get("aspect_ratio") or "9:16",
                    "n_frames": payload.get("n_frames") or "8",
                    "remove_watermark": bool(payload.get("remove_watermark", True)),
                }
            if not remaining:
                break

        if remaining:
            raise ValueError(
                f"Missing Sora prompts for video_index={sorted(remaining)} in stored SORA_REQUEST artifacts"
            )

        # Submit only the missing videos (fail-fast).
        videos_to_submit: list[dict[str, Any]] = []
        for idx in missing:
            spec = dict(resolved_specs[idx])
            spec["video_index"] = idx
            videos_to_submit.append(spec)

        tool = SoraGenerationTool(
            run_id=str(run_id), api_key=getattr(settings, "openai_api_key", None)
        )
        task_ids, task_meta, stop_error = await tool._submit_openai_videos(
            videos=videos_to_submit,
            aspect_ratio=str(submission_params.get("aspect_ratio") or "9:16"),
            n_frames=str(submission_params.get("n_frames") or "8"),
        )
        if stop_error:
            submitted = len(task_ids)
            raise ValueError(
                f"Sora repair submission failed after {submitted}/{len(videos_to_submit)} tasks. {stop_error}"
            )
        if not task_ids:
            raise ValueError("Sora repair submission failed: no tasks were submitted")

        # Build a new active manifest mapping one task_id per index (existing or newly submitted).
        mapping: dict[str, dict[str, Any]] = {}

        # Existing clips.
        for idx, clip in clips_by_index.items():
            if idx in missing:
                continue
            clip_meta = getattr(clip, "artifact_metadata", None) or {}
            task_id = clip_meta.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            entry: dict[str, Any] = {"video_index": idx}
            for key in ("topic", "sign", "object_name"):
                if key in clip_meta:
                    entry[key] = clip_meta[key]
            mapping[task_id] = entry

        # Newly submitted tasks.
        for task_id, meta in task_meta.items():
            entry = dict(meta) if isinstance(meta, dict) else {}
            entry.setdefault("video_index", entry.get("video_index", 0))
            mapping[str(task_id)] = entry

        index_set: set[int] = set()
        for value in mapping.values():
            idx = None
            try:
                idx = int(value.get("video_index"))
            except (TypeError, ValueError):
                idx = None
            if idx is None:
                continue
            index_set.add(idx)

        expected_index_set = set(range(expected_count))
        if index_set != expected_index_set:
            raise ValueError(
                "Refusing to write active manifest with missing/mismatched indices "
                f"(expected={sorted(expected_index_set)}, got={sorted(index_set)})"
            )
        if len(mapping) != expected_count:
            raise ValueError(
                f"Refusing to write active manifest with {len(mapping)} tasks (expected {expected_count})"
            )

        await artifact_repo.create_async(
            run_id=run_id,
            persona="producer",
            artifact_type=ArtifactType.CLIP_MANIFEST,
            content=json.dumps(mapping),
            metadata={
                "type": "task_metadata_mapping",
                "task_count": expected_count,
                "repair": True,
                "resubmitted_video_indexes": missing,
            },
        )

        await run_repo.add_artifact_async(run_id, "pending_task_ids", list(mapping.keys()))
        await run_repo.add_artifact_async(run_id, "repair_resubmitted_video_indexes", missing)
        await run_repo.update_async(
            run_id,
            status=RunStatus.AWAITING_VIDEO_GENERATION.value,
            current_step="wait_for_videos",
            error=None,
        )
        await session.commit()

        updated = await run_repo.get_async(run_id)
        return WorkflowResult(
            run_id=str(run_id),
            status=updated.status if updated else RunStatus.AWAITING_VIDEO_GENERATION.value,
            current_step=(
                updated.current_step or "wait_for_videos" if updated else "wait_for_videos"
            ),
            error=updated.error if updated else None,
            artifacts=updated.artifacts or {} if updated else {},
        )


def _extract_objects_from_artifacts(artifacts: dict[str, Any]) -> list[str]:
    overlays = artifacts.get("overlays")
    if isinstance(overlays, list):
        texts = [
            str(item.get("text"))
            for item in overlays
            if isinstance(item, dict) and item.get("text")
        ]
        if texts:
            return texts

    ideas_structured = artifacts.get("ideas_structured")
    if isinstance(ideas_structured, dict):
        ideas = ideas_structured.get("ideas")
        if isinstance(ideas, list):
            objects = [
                str(item.get("object"))
                for item in ideas
                if isinstance(item, dict) and item.get("object")
            ]
            if objects:
                return objects

    return []


def _extract_texts_from_artifacts(artifacts: dict[str, Any]) -> list[str]:
    texts = artifacts.get("texts")
    if isinstance(texts, list):
        return [str(t) for t in texts if t]
    overlays = artifacts.get("overlays")
    if isinstance(overlays, list):
        extracted: list[str] = []
        for item in overlays:
            if isinstance(item, dict) and item.get("text"):
                extracted.append(str(item["text"]))
            elif isinstance(item, str) and item.strip():
                extracted.append(item.strip())
        if extracted:
            return extracted
    return []


def _infer_template_for_repair(project: str, artifacts: dict[str, Any]) -> str:
    """Infer the Remotion template to use for a repair render.

    We intentionally prefer the project name over heuristic artifact inspection:
    both AISMR and motivational runs store `overlays`, but they have different semantics.
    """

    key = (project or "").strip().lower()
    if key == "aismr":
        return "aismr"
    if key == "motivational":
        return "motivational"
    raise ValueError(f"Unknown project for repair_render: {project!r}")


def _latest_render_job_id(artifacts: list[Any]) -> str | None:
    for artifact in reversed(artifacts):
        if getattr(artifact, "artifact_type", None) != ArtifactType.EDITOR_OUTPUT.value:
            continue
        meta = getattr(artifact, "artifact_metadata", None) or {}
        job_id = meta.get("render_job_id")
        if isinstance(job_id, str) and job_id:
            return job_id
    return None


def _latest_rendered_video_url(artifacts: list[Any]) -> str | None:
    for artifact in reversed(artifacts):
        if getattr(artifact, "artifact_type", None) != ArtifactType.RENDERED_VIDEO.value:
            continue
        url = getattr(artifact, "uri", None)
        if isinstance(url, str) and url:
            return url
    return None


def _result_from_run(run: Any) -> WorkflowResult:
    return WorkflowResult(
        run_id=str(run.id),
        status=run.status,
        current_step=run.current_step or None,
        error=run.error,
        artifacts=run.artifacts or {},
    )


async def repair_render(run_id: UUID, *, force: bool = False) -> WorkflowResult:
    """Re-submit Remotion render using existing clips + overlays (no new LLM call)."""
    provider = effective_remotion_provider(settings)
    if provider != "real":
        raise ValueError("repair_render requires REMOTION_PROVIDER=real (render service available)")

    SessionLocal = get_async_session_factory()
    async with _session_ctx(SessionLocal) as session:
        run_repo = RunRepository(session)
        artifact_repo = ArtifactRepository(session)

        run = await run_repo.get_for_update_async(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        allowed_statuses = {RunStatus.FAILED.value, RunStatus.AWAITING_RENDER.value}
        if run.status not in allowed_statuses and not force:
            raise ValueError(
                "repair_render is only allowed when a run is awaiting render or failed "
                f"(run.status={run.status})"
            )

        artifacts = await artifact_repo.get_by_run_async(run_id)
        editor_outputs = [
            a
            for a in artifacts
            if getattr(a, "artifact_type", None) == ArtifactType.EDITOR_OUTPUT.value
        ]
        if not editor_outputs:
            raise ValueError(
                "repair_render requires an existing editor_output artifact (safety-approved)."
            )

        clip_urls = select_latest_video_clip_urls(artifacts)
        if not clip_urls:
            raise ValueError(f"No video clips found for run {run_id}")

        resolved_clip_urls: list[str] = []
        for url in clip_urls:
            normalized = normalize_transcoded_url(str(url))
            if normalized and normalized != url:
                resolved_clip_urls.append(normalized)
                continue
            resolved = await resolve_s3_uri_async(str(url))
            resolved_clip_urls.append(normalize_transcoded_url(resolved) or resolved)

        artifacts_payload = run.artifacts or {}
        project_name = run.workflow_name or "aismr"
        template = _infer_template_for_repair(project_name, artifacts_payload)
        objects = _extract_objects_from_artifacts(artifacts_payload)
        texts = _extract_texts_from_artifacts(artifacts_payload)

        if template == "aismr" and not objects:
            raise ValueError("repair_render could not extract object overlays for AISMR template")
        if template == "motivational" and not texts:
            raise ValueError("repair_render could not extract texts for motivational template")

        aspect_ratio = "9:16"
        try:
            project_cfg = load_project(project_name)
            aspect_ratio = getattr(project_cfg.specs, "aspect_ratio", aspect_ratio) or aspect_ratio
        except Exception as exc:
            logger.debug("repair_render project config lookup failed: %s", exc)

        tool = RemotionRenderTool(run_id=str(run_id), project=project_name)
        result = await tool.async_run_impl(
            clips=resolved_clip_urls,
            template=template,
            objects=objects or None,
            texts=texts or None,
            aspect_ratio=aspect_ratio,
        )

        job_id = result.get("job_id")
        if not job_id:
            raise ValueError("repair_render failed: Remotion response missing job_id")

        await artifact_repo.create_async(
            run_id=run_id,
            persona="editor",
            artifact_type=ArtifactType.EDITOR_OUTPUT,
            content=f"Repair render submitted (job_id={job_id})",
            metadata={
                "step": "editor",
                "clip_count": len(resolved_clip_urls),
                "render_job_id": job_id,
                "source": "repair_render",
            },
        )
        await run_repo.update_async(
            run_id,
            status=RunStatus.AWAITING_RENDER.value,
            current_step="wait_for_render",
            error=None,
        )

        if settings.workflow_dispatcher == "db":
            from myloware.storage.repositories import JobRepository
            from myloware.workers.job_types import (
                JOB_REMOTION_POLL,
                idempotency_remotion_poll,
            )

            job_repo = JobRepository(session)
            try:
                await job_repo.enqueue_async(
                    JOB_REMOTION_POLL,
                    run_id=run_id,
                    payload={},
                    idempotency_key=idempotency_remotion_poll(run_id, str(job_id)),
                    max_attempts=180,
                )
            except ValueError:
                pass

        await session.commit()

        return WorkflowResult(
            run_id=str(run_id),
            status=RunStatus.AWAITING_RENDER.value,
            current_step="wait_for_render",
            error=None,
        )


async def resume_run(
    run_id: UUID,
    *,
    action: str = "auto",
    approve_publish: bool = False,
    force: bool = False,
    checkpoint_id: str | None = None,
    video_indexes: list[int] | None = None,
) -> dict[str, Any]:
    """Resume or rewind a run using existing artifacts (no automatic retries)."""
    SessionLocal = get_async_session_factory()
    async with _session_ctx(SessionLocal) as session:
        run_repo = RunRepository(session)
        artifact_repo = ArtifactRepository(session)
        run = await run_repo.get_async(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")
        artifacts = await artifact_repo.get_by_run_async(run_id)

    action = (action or "auto").strip().lower()
    message: str | None = None

    if action == "auto":
        if run.status == RunStatus.AWAITING_VIDEO_GENERATION.value:
            action = "videos"
        elif run.status == RunStatus.AWAITING_RENDER.value:
            action = "render"
        elif run.status == RunStatus.AWAITING_PUBLISH_APPROVAL.value:
            action = "publish"
        elif run.status == RunStatus.FAILED.value:
            if _latest_rendered_video_url(artifacts):
                action = "render"
            elif select_latest_video_clip_urls(artifacts):
                action = "videos"
            else:
                return {
                    "action": None,
                    "message": "Run is failed with no clips/rendered video to resume",
                    "result": _result_from_run(run),
                }
        else:
            return {
                "action": None,
                "message": f"Run status '{run.status}' is not resumable",
                "result": _result_from_run(run),
            }

    if action == "fork-from-clips":
        result = await fork_from_clips(run_id, checkpoint_id=checkpoint_id, force=force)
        return {"action": "fork-from-clips", "message": "Forked from clips", "result": result}

    if action == "repair-render":
        result = await repair_render(run_id, force=force)
        return {"action": "repair-render", "message": "Render re-submitted", "result": result}

    if action == "repair-videos":
        result = await repair_sora_clips(run_id, video_indexes=video_indexes, force=force)
        return {"action": "repair-videos", "message": "Videos re-submitted", "result": result}

    if action == "publish":
        if run.status != RunStatus.AWAITING_PUBLISH_APPROVAL.value and not force:
            return {
                "action": "publish",
                "message": f"Run status '{run.status}' is not awaiting publish approval",
                "result": _result_from_run(run),
            }
        if not approve_publish:
            return {
                "action": "publish",
                "message": "Publish gate awaiting approval (set approve_publish=true)",
                "result": _result_from_run(run),
            }
        result = await continue_after_publish_approval(run_id, approved=True)
        return {"action": "publish", "message": "Publish approved", "result": result}

    if action == "videos":
        if (
            run.status
            not in {
                RunStatus.AWAITING_VIDEO_GENERATION.value,
                RunStatus.FAILED.value,
            }
            and not force
        ):
            return {
                "action": "videos",
                "message": f"Run status '{run.status}' is not awaiting video generation",
                "result": _result_from_run(run),
            }
        if not select_latest_video_clip_urls(artifacts):
            return {
                "action": "videos",
                "message": "No video clips found to resume",
                "result": _result_from_run(run),
            }
        from myloware.workflows.langgraph.resume import resume_after_videos

        await resume_after_videos(run_id, raise_on_error=True, fail_run_on_error=False)
        async with _session_ctx(SessionLocal) as session:
            run_repo = RunRepository(session)
            updated = await run_repo.get_async(run_id)
        return {
            "action": "videos",
            "message": "Resumed after videos",
            "result": _result_from_run(updated) if updated else _result_from_run(run),
        }

    if action == "render":
        if (
            run.status
            not in {
                RunStatus.AWAITING_RENDER.value,
                RunStatus.FAILED.value,
            }
            and not force
        ):
            return {
                "action": "render",
                "message": f"Run status '{run.status}' is not awaiting render",
                "result": _result_from_run(run),
            }
        rendered_url = _latest_rendered_video_url(artifacts)
        if rendered_url:
            from myloware.workflows.langgraph.resume import resume_after_render

            final_url = normalize_remotion_output_url(rendered_url) or rendered_url
            await resume_after_render(
                run_id, final_url, raise_on_error=True, fail_run_on_error=False
            )
            async with _session_ctx(SessionLocal) as session:
                run_repo = RunRepository(session)
                updated = await run_repo.get_async(run_id)
            return {
                "action": "render",
                "message": "Resumed after render",
                "result": _result_from_run(updated) if updated else _result_from_run(run),
            }

        job_id = _latest_render_job_id(artifacts)
        if not job_id:
            return {
                "action": "render",
                "message": "No render job id found to resume",
                "result": _result_from_run(run),
            }

        provider = get_render_provider()
        job = await provider.get_status(job_id)
        if job.status == RenderStatus.COMPLETED and job.artifact_url:
            from myloware.workflows.langgraph.resume import resume_after_render

            final_url = normalize_remotion_output_url(job.artifact_url) or job.artifact_url
            await resume_after_render(
                run_id, final_url, raise_on_error=True, fail_run_on_error=False
            )
            async with _session_ctx(SessionLocal) as session:
                run_repo = RunRepository(session)
                updated = await run_repo.get_async(run_id)
            return {
                "action": "render",
                "message": "Resumed after render",
                "result": _result_from_run(updated) if updated else _result_from_run(run),
            }

        if job.status == RenderStatus.FAILED:
            message = f"Render failed: {job.error or 'unknown'}"
            return {"action": "render", "message": message, "result": _result_from_run(run)}

        return {
            "action": "render",
            "message": f"Render still {job.status}",
            "result": _result_from_run(run),
        }

    raise ValueError(f"Unknown resume action: {action}")


async def fork_from_clips(
    run_id: UUID, checkpoint_id: str | None = None, *, force: bool = False
) -> WorkflowResult:
    """Operator-only recovery: fork a run from an earlier wait_for_videos checkpoint.

    Reuses existing VIDEO_CLIP artifacts (no new Sora calls) and resumes the
    wait_for_videos interrupt via LangGraph time travel.

    Args:
        run_id: Existing run/thread UUID.
        checkpoint_id: Optional checkpoint_id to fork from. If omitted, the most
            recent checkpoint waiting for Sora is used.

    Returns:
        WorkflowResult reflecting the updated run after resume.

    Raises:
        ValueError if no suitable checkpoint or clips are found.
    """
    from myloware.workflows.langgraph.graph import ensure_checkpointer_initialized

    if not settings.database_url.startswith("sqlite"):
        await ensure_checkpointer_initialized()

    graph = get_graph()
    thread_id = str(run_id)
    base_config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    # Collect existing video clips from artifacts and validate run state.
    SessionLocal = get_async_session_factory()
    async with _session_ctx(SessionLocal) as session:
        run_repo = RunRepository(session)
        run = await run_repo.get_async(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")
        if not force:
            replayable = {
                RunStatus.FAILED.value,
                RunStatus.AWAITING_VIDEO_GENERATION.value,
                RunStatus.AWAITING_RENDER.value,
                RunStatus.AWAITING_PUBLISH_APPROVAL.value,
                RunStatus.AWAITING_PUBLISH.value,
                RunStatus.REJECTED.value,
            }
            if run.status not in replayable:
                raise ValueError(
                    f"Run {run_id} status '{run.status}' is not replayable without force"
                )
        artifact_repo = ArtifactRepository(session)
        artifacts = await artifact_repo.get_by_run_async(run_id)
        video_urls = select_latest_video_clip_urls(artifacts)

    if not video_urls:
        has_any_clips = any(
            getattr(a, "artifact_type", None) == ArtifactType.VIDEO_CLIP.value for a in artifacts
        )
        if has_any_clips:
            raise ValueError(f"No video clips matched the latest clip_manifest for run {run_id}")
        raise ValueError(f"No VIDEO_CLIP artifacts found for run {run_id}")

    # Find checkpoint to fork from.
    from datetime import datetime

    def _checkpoint_ts(candidate: Any) -> datetime | None:
        raw = None
        checkpoint_blob = getattr(candidate, "checkpoint", None)
        if isinstance(checkpoint_blob, dict):
            raw = checkpoint_blob.get("ts")
        metadata_blob = getattr(candidate, "metadata", None)
        if raw is None and isinstance(metadata_blob, dict):
            raw = metadata_blob.get("created_at") or metadata_blob.get("ts")
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                return None
        if isinstance(raw, datetime):
            return raw
        return None

    selected_checkpoint = None
    selected_ts: datetime | None = None
    async for checkpoint in graph.aget_state_history(base_config):
        cp_id = None
        if getattr(checkpoint, "config", None):
            cp_id = checkpoint.config.get("configurable", {}).get("checkpoint_id")

        if checkpoint_id and cp_id != checkpoint_id:
            continue

        values = dict(getattr(checkpoint, "values", {}) or {})
        current_step = values.get("current_step")
        if current_step is None and isinstance(values.get("state"), dict):
            current_step = values["state"].get("current_step")

        interrupts = getattr(checkpoint, "interrupts", None) or []
        waiting_for_sora = False
        for intr in interrupts:
            intr_val = getattr(intr, "value", None) or {}
            if isinstance(intr_val, dict) and intr_val.get("waiting_for") == "sora_webhook":
                waiting_for_sora = True
                break

        if checkpoint_id:
            # Caller selected a specific checkpoint; require it to be at a Sora wait boundary.
            if not waiting_for_sora and current_step != "wait_for_videos":
                raise ValueError(
                    f"Checkpoint {checkpoint_id} is not a wait_for_videos/Sora checkpoint"
                )
            selected_checkpoint = checkpoint
            break

        # Auto-select the most recent Sora-wait checkpoint.
        if waiting_for_sora:
            candidate_ts = _checkpoint_ts(checkpoint)
            if selected_checkpoint is None:
                selected_checkpoint = checkpoint
                selected_ts = candidate_ts
            elif candidate_ts and (selected_ts is None or candidate_ts > selected_ts):
                selected_checkpoint = checkpoint
                selected_ts = candidate_ts

    if not selected_checkpoint:
        raise ValueError(
            f"No sora_webhook wait checkpoint found for run {run_id}. "
            "Provide --checkpoint-id from /v2/runs/{id}/history to force a fork."
        )

    if not getattr(selected_checkpoint, "config", None):
        raise ValueError(f"Selected checkpoint missing config for run {run_id}")

    # Resume wait_for_videos interrupt on the selected checkpoint. This time-travels
    # the run without re-submitting Sora tasks.
    interrupts = getattr(selected_checkpoint, "interrupts", None) or []
    interrupt_id = None
    for intr in interrupts:
        intr_val = getattr(intr, "value", None) or {}
        if isinstance(intr_val, dict) and intr_val.get("waiting_for") == "sora_webhook":
            interrupt_id = getattr(intr, "id", None) or getattr(intr, "interrupt_id", None)
            break
    if not interrupt_id:
        raise ValueError(
            f"Selected checkpoint has no sora_webhook interrupt for run {run_id}; refusing to resume"
        )

    resume_payload: dict[str, Any] = {interrupt_id: {"video_urls": video_urls}}

    await graph.ainvoke(
        Command(resume=resume_payload),
        config=selected_checkpoint.config,
        durability="sync",
    )

    # Return DB projection.
    SessionLocal = get_async_session_factory()
    async with _session_ctx(SessionLocal) as session:
        run_repo = RunRepository(session)
        updated_run = await run_repo.get_async(run_id)

    return WorkflowResult(
        run_id=str(run_id),
        status=updated_run.status if updated_run else RunStatus.FAILED.value,
        current_step=updated_run.current_step or "unknown" if updated_run else "unknown",
        error=updated_run.error if updated_run else None,
    )


# Helper to support both callable session factories and preconstructed context managers
def _session_ctx(session_factory: Any) -> Any:
    # Already a context manager
    if hasattr(session_factory, "__aenter__") and hasattr(session_factory, "__aexit__"):
        return session_factory
    # Callable factory -> invoke to get context manager
    if callable(session_factory):
        candidate = session_factory()
        # If invocation returns a coroutine, await it to get the context manager
        if asyncio.iscoroutine(candidate):
            # Return an async context manager wrapper that awaits the coroutine once
            class _AwaitableCtx:
                def __init__(self, coro: Any) -> None:
                    self._coro = coro
                    self._ctx = None

                async def __aenter__(self) -> Any:
                    self._ctx = await self._coro
                    return await self._ctx.__aenter__()

                async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
                    return await self._ctx.__aexit__(exc_type, exc, tb)

            return _AwaitableCtx(candidate)
        return candidate
    # Fallback: assume it's already a session
    return session_factory


async def create_pending_run(
    run_repo: RunRepository,
    workflow_name: str,
    brief: str,
    user_id: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
) -> UUID:
    """Create a new pending run entry in the database."""
    from myloware.config.projects import load_project

    load_project(workflow_name)  # Validate project exists

    run = await run_repo.create_async(
        workflow_name=workflow_name,
        user_id=user_id,
        input=brief,
        status=RunStatus.PENDING,
        telegram_chat_id=telegram_chat_id,
    )
    await run_repo.session.commit()
    logger.info("Created pending run: %s", run.id)
    return run.id


async def _wait_for_run_visibility(
    run_id: UUID,
    session_factory: Callable[[], async_sessionmaker[AsyncSession]] | Any,
    max_attempts: int = 10,
    delay: float = 0.05,
) -> Any | None:
    """Poll until a run is visible to a new session.

    SQLite's default isolation can hide freshly committed rows from
    connections that start immediately afterward (especially when mixing
    sync TestClient and async sessions). This helper is used by
    background tasks to avoid failing when the row is not yet readable.
    """
    for attempt in range(max_attempts):
        async with _session_ctx(session_factory) as verify_session:
            verify_repo = RunRepository(verify_session)
            run = await verify_repo.get_async(run_id)
            if run is not None:
                return run
        if attempt < max_attempts - 1:
            await asyncio.sleep(delay)
    return None


async def run_workflow_async(
    client: LlamaStackClient,
    run_id: UUID,
    vector_db_id: str,
    notifier: Any | None = None,  # TelegramNotifier | None - avoid circular import
) -> None:
    """Execute workflow for an existing pending run using LangGraph.

    This is the async version that should be used in FastAPI background tasks.

    Note: The run should already be verified as visible before this task is started.
    This function assumes the run exists and will fail if it doesn't.
    """
    SessionLocal = get_async_session_factory()
    session_factory = SessionLocal if callable(SessionLocal) else (lambda: SessionLocal)

    # Ensure the run is visible before starting the workflow (handles SQLite read isolation)
    visible_run = await _wait_for_run_visibility(run_id, session_factory)
    if visible_run is None:
        logger.error("Run not visible after polling; aborting workflow start: %s", run_id)
        return

    async with _session_ctx(SessionLocal) as session:
        run_repo = RunRepository(session)

        # Re-fetch the run within the session used for the workflow
        run = await run_repo.get_async(run_id)
        if run is None:
            logger.error("Run not found for async execution: %s", run_id)
            return

        from myloware.workflows.admission import require_legacy_monthly_engine_allowed

        require_legacy_monthly_engine_allowed(run.workflow_name)

        try:
            await run_repo.update_async(run_id, status=RunStatus.RUNNING.value)
            await session.commit()

            logger.info("Starting LangGraph workflow %s (run_id=%s)", run.workflow_name, run_id)

            # Ensure checkpointer is initialized on this loop
            from myloware.workflows.langgraph.graph import ensure_checkpointer_initialized

            if not settings.database_url.startswith("sqlite"):
                await ensure_checkpointer_initialized()

            # Get LangGraph instance
            graph = get_graph()
            thread_id = str(run_id)
            config = {"configurable": {"thread_id": thread_id}}

            # Initial state
            initial_state: VideoWorkflowState = {
                "run_id": str(run_id),
                "project": str(run.workflow_name),
                "brief": str(run.input or ""),
                "vector_db_id": vector_db_id,
                "status": RunStatus.RUNNING.value,
                "current_step": "ideation",
            }

            # Invoke LangGraph workflow with durability="sync" for parity/Postgres invocations
            await graph.ainvoke(initial_state, config=config, durability="sync")

            logger.info("LangGraph workflow completed for run %s", run_id)

        except Exception as exc:
            logger.exception("LangGraph workflow failed for run %s: %s", run_id, exc)
            await run_repo.update_async(run_id, status=RunStatus.FAILED.value, error=str(exc))
            await session.commit()


def run_workflow(
    client: LlamaStackClient,
    brief: str,
    vector_db_id: str,
    run_repo: RunRepository,
    artifact_repo: ArtifactRepository,
    workflow_name: str = "aismr",
    user_id: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
) -> WorkflowResult:
    """Execute workflow synchronously using LangGraph (for CLI use).

    This is a sync wrapper around the async LangGraph execution.
    Note: run_repo and artifact_repo are sync repositories for CLI compatibility.
    """
    import anyio

    # Create pending run (sync)
    from myloware.storage.database import get_session_factory

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        sync_run_repo = RunRepository(session)
        run = sync_run_repo.create(
            workflow_name=workflow_name,
            user_id=user_id,
            input=brief,
            status=RunStatus.PENDING,
            telegram_chat_id=telegram_chat_id,
        )
        run_id = run.id
        session.commit()

    # Execute workflow (async, but run in sync context)
    async def _run() -> None:
        await run_workflow_async(
            client=client,
            run_id=run_id,
            vector_db_id=vector_db_id,
        )

    anyio.run(_run)

    # Get final run state (sync)
    with SessionLocal() as session:
        sync_run_repo = RunRepository(session)
        run = sync_run_repo.get(run_id)
        if not run:
            return WorkflowResult(
                run_id=str(run_id),
                status=RunStatus.FAILED.value,
                current_step="unknown",
                error="Run not found after execution",
            )

        return WorkflowResult(
            run_id=str(run_id),
            status=run.status,
            artifacts=run.artifacts or {},
            current_step=run.current_step or "unknown",
            error=run.error,
        )


async def continue_after_ideation(
    run_id: UUID, approved: bool = True, comment: str | None = None
) -> WorkflowResult:
    """Continue workflow after ideation approval using LangGraph resume."""
    from myloware.storage.database import get_async_session_factory
    from myloware.workflows.langgraph.graph import ensure_checkpointer_initialized

    SessionLocal = get_async_session_factory()

    async with _session_ctx(SessionLocal) as session:
        run_repo = RunRepository(session)

        run = await run_repo.get_async(run_id)
        if not run:
            return WorkflowResult(
                run_id=str(run_id),
                status=RunStatus.FAILED.value,
                current_step="unknown",
                error="Run not found",
            )

        if not settings.database_url.startswith("sqlite"):
            await ensure_checkpointer_initialized()

        graph = get_graph()
        thread_id = str(run_id)
        config = {"configurable": {"thread_id": thread_id}}

        # Resume from ideation_approval node with approval. Prefer targeting the
        # exact pending interrupt (if present) to guarantee the payload reaches
        # ideation_approval_node. Without an interrupt id mapping, LangGraph may
        # ignore the payload when multiple interrupts are pending.
        resume_payload = {"approved": approved, "ideas_approved": approved}
        if comment:
            resume_payload["comment"] = comment

        try:
            graph_state = await graph.aget_state(config)
            interrupts = getattr(graph_state, "interrupts", None) or []
            interrupt_id = None
            if interrupts:
                first_interrupt = interrupts[0]
                interrupt_id = getattr(first_interrupt, "id", None) or getattr(
                    first_interrupt, "interrupt_id", None
                )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug("Failed to fetch graph state before resume: %s", exc)
            interrupts = []
            interrupt_id = None

        resume_argument = {interrupt_id: resume_payload} if interrupt_id else resume_payload

        await graph.ainvoke(Command(resume=resume_argument), config=config, durability="sync")

        # Get updated run state
        updated_run = await run_repo.get_async(run_id)
        return WorkflowResult(
            run_id=str(run_id),
            status=updated_run.status if updated_run else RunStatus.FAILED.value,
            artifacts=updated_run.artifacts or {} if updated_run else {},
            current_step=updated_run.current_step or "unknown" if updated_run else "unknown",
            error=updated_run.error if updated_run else None,
        )


async def continue_after_producer(run_id: UUID) -> WorkflowResult:
    """Continue workflow after producer completes (webhook callback).

    This is called from webhook handlers when videos are ready.
    """
    from myloware.storage.database import get_async_session_factory

    SessionLocal = get_async_session_factory()

    async with _session_ctx(SessionLocal) as session:
        artifact_repo = ArtifactRepository(session)
        artifacts = await artifact_repo.get_by_run_async(run_id)
        video_clips = select_latest_video_clip_urls(artifacts)

        if not video_clips:
            logger.warning("No video clips found for run %s", run_id)
            return WorkflowResult(
                run_id=str(run_id),
                status=RunStatus.FAILED.value,
                current_step="production",
                error="No video clips found",
            )

        from myloware.workflows.langgraph.graph import ensure_checkpointer_initialized

        if not settings.database_url.startswith("sqlite"):
            await ensure_checkpointer_initialized()

        graph = get_graph()
        thread_id = str(run_id)
        config = {"configurable": {"thread_id": thread_id}}

        # Resume from the pending interrupt at wait_for_videos with video URLs.
        # Prefer targeting the specific interrupt id so LangGraph applies the payload.
        resume_data = {"video_urls": video_clips, "status": RunStatus.RUNNING.value, "error": None}
        interrupt_id = None
        try:
            graph_state = await graph.aget_state(config)
            interrupts = getattr(graph_state, "interrupts", None) or []
            for intr in interrupts:
                intr_val = getattr(intr, "value", None) or {}
                if isinstance(intr_val, dict) and intr_val.get("waiting_for") == "sora_webhook":
                    interrupt_id = getattr(intr, "id", None) or getattr(intr, "interrupt_id", None)
                    break
            if not interrupt_id:
                raise ValueError(
                    f"No sora_webhook interrupt found for run {run_id}; refusing to resume"
                )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug("Failed to fetch graph state before producer resume: %s", exc)
            raise

        resume_argument = {interrupt_id: resume_data}
        await graph.ainvoke(Command(resume=resume_argument), config=config, durability="sync")

        run_repo = RunRepository(session)
        updated_run = await run_repo.get_async(run_id)
        return WorkflowResult(
            run_id=str(run_id),
            status=updated_run.status if updated_run else RunStatus.FAILED.value,
            current_step=updated_run.current_step or "unknown" if updated_run else "unknown",
            error=updated_run.error if updated_run else None,
        )


async def continue_after_render(run_id: UUID, video_url: str) -> WorkflowResult:
    """Continue workflow after render completes (Remotion webhook or poller)."""
    from myloware.storage.database import get_async_session_factory

    SessionLocal = get_async_session_factory()

    async with _session_ctx(SessionLocal) as session:
        run_repo = RunRepository(session)

        from myloware.workflows.langgraph.graph import ensure_checkpointer_initialized

        if not settings.database_url.startswith("sqlite"):
            await ensure_checkpointer_initialized()

        graph = get_graph()
        thread_id = str(run_id)
        config = {"configurable": {"thread_id": thread_id}}

        resume_data = {"video_url": video_url}
        interrupt_id = None
        try:
            graph_state = await graph.aget_state(config)
            interrupts = getattr(graph_state, "interrupts", None) or []
            for intr in interrupts:
                intr_val = getattr(intr, "value", None) or {}
                if isinstance(intr_val, dict) and intr_val.get("waiting_for") == "remotion_webhook":
                    interrupt_id = getattr(intr, "id", None) or getattr(intr, "interrupt_id", None)
                    break
            if not interrupt_id:
                raise ValueError(
                    f"No remotion_webhook interrupt found for run {run_id}; refusing to resume"
                )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug("Failed to fetch graph state before render resume: %s", exc)
            raise

        resume_argument = {interrupt_id: resume_data}
        await graph.ainvoke(Command(resume=resume_argument), config=config, durability="sync")

        updated_run = await run_repo.get_async(run_id)
        return WorkflowResult(
            run_id=str(run_id),
            status=updated_run.status if updated_run else RunStatus.FAILED.value,
            current_step=updated_run.current_step or "unknown" if updated_run else "unknown",
            error=updated_run.error if updated_run else None,
        )


async def continue_after_publish_approval(
    run_id: UUID, approved: bool = True, comment: str | None = None
) -> WorkflowResult:
    """Continue workflow after publish approval using LangGraph resume."""
    from myloware.storage.database import get_async_session_factory

    SessionLocal = get_async_session_factory()

    async with _session_ctx(SessionLocal) as session:
        run_repo = RunRepository(session)

        run = await run_repo.get_async(run_id)
        if not run:
            return WorkflowResult(
                run_id=str(run_id),
                status=RunStatus.FAILED.value,
                current_step="unknown",
                error="Run not found",
            )

        from myloware.workflows.langgraph.graph import ensure_checkpointer_initialized

        if not settings.database_url.startswith("sqlite"):
            await ensure_checkpointer_initialized()

        graph = get_graph()
        thread_id = str(run_id)
        config = {"configurable": {"thread_id": thread_id}}

        # Resume from publish_approval node with approval decision.
        # publish_approval_node reads `approved`/`comment` from the interrupt payload.
        resume_data: dict[str, Any] = {"approved": approved}
        if comment:
            resume_data["comment"] = comment
        interrupt_id = None
        try:
            graph_state = await graph.aget_state(config)
            interrupts = getattr(graph_state, "interrupts", None) or []
            if interrupts:
                first_interrupt = interrupts[0]
                interrupt_id = getattr(first_interrupt, "id", None) or getattr(
                    first_interrupt, "interrupt_id", None
                )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug("Failed to fetch graph state before publish approval resume: %s", exc)

        resume_argument = {interrupt_id: resume_data} if interrupt_id else resume_data
        await graph.ainvoke(Command(resume=resume_argument), config=config, durability="sync")

        # Get updated run state
        updated_run = await run_repo.get_async(run_id)
        return WorkflowResult(
            run_id=str(run_id),
            status=updated_run.status if updated_run else RunStatus.FAILED.value,
            artifacts=updated_run.artifacts or {} if updated_run else {},
            current_step=updated_run.current_step or "unknown" if updated_run else "unknown",
            error=updated_run.error if updated_run else None,
        )


async def continue_after_publish(run_id: UUID, published_urls: list[str]) -> WorkflowResult:
    """Continue workflow after publish completion (poller/webhook)."""
    from myloware.storage.database import get_async_session_factory

    SessionLocal = get_async_session_factory()

    async with _session_ctx(SessionLocal) as session:
        run_repo = RunRepository(session)

        from myloware.workflows.langgraph.graph import ensure_checkpointer_initialized

        if not settings.database_url.startswith("sqlite"):
            await ensure_checkpointer_initialized()

        graph = get_graph()
        thread_id = str(run_id)
        config = {"configurable": {"thread_id": thread_id}}

        # Target the specific interrupt if present
        resume_data = {"published_urls": published_urls, "publish_complete": True}
        interrupt_id = None
        try:
            graph_state = await graph.aget_state(config)
            interrupts = getattr(graph_state, "interrupts", None) or []
            for intr in interrupts:
                intr_val = getattr(intr, "value", None) or {}
                if isinstance(intr_val, dict) and intr_val.get("waiting_for") == "publish_webhook":
                    interrupt_id = getattr(intr, "id", None) or getattr(intr, "interrupt_id", None)
                    break
            if not interrupt_id:
                raise ValueError(
                    f"No publish_webhook interrupt found for run {run_id}; refusing to resume"
                )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug("Failed to fetch graph state before publish resume: %s", exc)
            raise

        resume_argument = {interrupt_id: resume_data}
        await graph.ainvoke(Command(resume=resume_argument), config=config, durability="sync")

        updated_run = await run_repo.get_async(run_id)
        return WorkflowResult(
            run_id=str(run_id),
            status=updated_run.status if updated_run else RunStatus.FAILED.value,
            current_step=updated_run.current_step or "unknown" if updated_run else "unknown",
            error=updated_run.error if updated_run else None,
        )
