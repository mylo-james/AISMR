"""Durable submission and collection of the 24 approved monthly media assets."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select

from myloware.providers.media import (
    FAKE_NARRATION_BATCH_MODEL,
    LOCAL_SCENE_MEDIA_MODEL,
    AssetResult,
    Submission,
    SubmissionUnknown,
    SubmitRejected,
)
from myloware.storage.models import Artifact, _utc_now
from myloware.storage.studio_models import (
    StudioAsset,
    StudioCostLedger,
    StudioDecision,
    StudioEvent,
    StudioPlannerRun,
    StudioReservation,
    StudioRun,
)
from myloware.storage.studio_store import StudioError, StudioStore
from myloware.studio.media import (
    MediaFetchPolicy,
    MediaVerificationError,
    fetch_verified_media,
    verify_media_file,
)
from myloware.studio.moderation import Moderator
from myloware.studio.voice_batch import (
    build_narration_text,
    build_scene_narration_text,
    canonical_narration_lines,
    scene_narration_lines,
)
from myloware.workflows.monthly import MonthlyPlan
from myloware.workflows.scenes import ScenePlan, StudioPlan, parse_plan


class AssetProvider(Protocol):
    async def submit(self, *, prompt: str, ordinal: int, operation_key: str) -> Submission: ...
    async def poll(self, request_id: str) -> AssetResult: ...


class NarrationBatchProvider(Protocol):
    async def submit_batch(
        self,
        *,
        lines: tuple[str, ...],
        operation_key: str,
        voice_profile: dict[str, object] | None = None,
    ) -> Submission: ...
    async def poll(self, request_id: str) -> Any: ...


def _json_timestamp_receipt(value: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Bound a JSON-native provider timing receipt before durable preservation."""
    if len(value) > 4096 or not all(_json_native(item) for item in value):
        raise MediaVerificationError("narration_timestamp_receipt_invalid")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MediaVerificationError("narration_timestamp_receipt_invalid") from exc
    if len(encoded.encode("utf-8")) > 1_000_000:
        raise MediaVerificationError("narration_timestamp_receipt_too_large")
    decoded = json.loads(encoded)
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise MediaVerificationError("narration_timestamp_receipt_invalid")
    return decoded


def _json_native(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_native(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_native(item) for key, item in value.items())
    return False


class MonthlyAssets:
    """Own the persisted boundary between an approved plan and the local editor.

    Database transactions are deliberately short.  Provider submission and media
    download happen after their durable state transitions have committed.
    """

    def __init__(
        self,
        store: StudioStore,
        video_provider: AssetProvider,
        voice_provider: NarrationBatchProvider,
        moderator: Moderator,
        policy: MediaFetchPolicy,
        renderer_preflight: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.video_provider = video_provider
        self.voice_provider = voice_provider
        self.moderator = moderator
        self.policy = policy
        self.renderer_preflight = renderer_preflight

    async def submit(self, run_id: UUID) -> None:
        """Create twelve video intents and one durable batch-narration intent."""
        await self._preflight_scene_submission(run_id)
        async with self.store.transaction() as session:
            run, plan = await self._approved_plan(session, run_id)
            # A new worker cannot know whether another process died between the
            # durable claim and the remote receipt.  Preserve that ambiguity.
            stranded = (
                await session.scalars(
                    select(StudioAsset).where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == run.revision,
                        StudioAsset.status == "submitting",
                    )
                )
            ).all()
            for asset in stranded:
                asset.status, asset.error_code = (
                    "submission_unknown",
                    "submission_unknown",
                )
            existing = (
                await session.scalars(
                    select(StudioAsset).where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == run.revision,
                    )
                )
            ).all()
            if not existing:
                for idea in plan.ideas:
                    operation = self._operation_key(run_id, run.revision, idea.ordinal, "video", 1)
                    local_video_metadata: dict[str, object] | None = None
                    if run.mode == "local":
                        archive = getattr(self.video_provider, "archive", None)
                        if archive is None:
                            raise StudioError("local_media_provider_unavailable", 503)
                        source = archive.video(idea.ordinal)
                        source_line = archive.source_title_lines()[idea.ordinal - 1]
                        local_video_metadata = {
                            "local_media_receipt": archive.receipt(),
                            "requested_prompt_sha256": sha256(
                                idea.visual_prompt.encode()
                            ).hexdigest(),
                            "source_title": source_line,
                            "source_title_sha256": sha256(source_line.encode()).hexdigest(),
                            "source_sha256": source.sha256,
                            "alignment_status": "ordinal_only_mismatch",
                        }
                    session.add(
                        StudioAsset(
                            run_id=run_id,
                            revision=run.revision,
                            ordinal=idea.ordinal,
                            kind="video",
                            attempt=1,
                            input_hash=sha256(idea.visual_prompt.encode()).hexdigest(),
                            operation_key=operation,
                            provider="video",
                            status="pending",
                            media_metadata=local_video_metadata,
                        )
                    )
                lines = self._narration_lines(plan)
                session.add(
                    StudioAsset(
                        run_id=run_id,
                        revision=run.revision,
                        ordinal=0,
                        kind="voice_batch",
                        attempt=1,
                        input_hash=sha256("\n".join(lines).encode()).hexdigest(),
                        operation_key=self._operation_key(
                            run_id, run.revision, 0, "voice_batch", 1
                        ),
                        provider="voice_batch",
                        status="pending",
                    )
                )
                await self.store.event(
                    session,
                    run,
                    "generating",
                    "asset_intents_created",
                    {"video_count": 12, "voice_batch_count": 1},
                )
                await self.store.event(
                    session,
                    run,
                    "generating",
                    "workflow_phase",
                    {
                        "revision": run.revision,
                        "component": "worker",
                        "role": "media_submission",
                        "state": "queued",
                        "counts": {"videos": 12, "narration_batches": 1},
                    },
                )
        # Re-read tasks outside the intent transaction.  Pending is the only state
        # that can start a provider call; a restart never repeats submitting work.
        async with self.store.factory() as session:
            run = await session.get(StudioRun, run_id)
            if run is None:
                return
            rows = (
                await session.scalars(
                    select(StudioAsset).where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == run.revision,
                        StudioAsset.status == "pending",
                    )
                )
            ).all()
        await asyncio.gather(*(self._submit_one(row.id) for row in rows))

    async def _preflight_scene_submission(self, run_id: UUID) -> None:
        """Require the renderer's pinned v2 preset before any paid media effect.

        The callback is intentionally outside a database transaction because it
        reaches the renderer. It runs only where this call could create a live
        provider intent or submit a pending live asset. Existing v1 and fixture
        paths retain their established behavior.
        """
        async with self.store.factory() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.status != "generating" or run.mode not in {"live", "local"}:
                return
            try:
                plan = parse_plan(run.plan)
                if not isinstance(plan, ScenePlan):
                    return
                profile = self._scene_execution_profile(plan, run)
            except (TypeError, ValueError) as exc:
                raise StudioError("scene_execution_profile_invalid") from exc
            if run.mode == "local":
                archive = getattr(self.video_provider, "archive", None)
                planner = await session.get(StudioPlannerRun, run_id)
                try:
                    receipt = archive.receipt() if archive is not None else None
                except (TypeError, ValueError) as exc:
                    raise StudioError("local_media_receipt_mismatch", 409) from exc
                if (
                    planner is None
                    or receipt is None
                    or planner.configuration.get("local_media_receipt") != receipt
                ):
                    raise StudioError("local_media_receipt_mismatch", 409)
            assets = (
                await session.scalars(
                    select(StudioAsset.status).where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == run.revision,
                    )
                )
            ).all()
            needs_effect = not assets or "pending" in assets
        if not needs_effect:
            return
        if self.renderer_preflight is None:
            raise StudioError("scene_renderer_preflight_unavailable", 503)
        try:
            await self.renderer_preflight(profile)
        except Exception as exc:
            raise StudioError("scene_renderer_preflight_failed", 503) from exc

    async def _submit_one(self, asset_id: UUID) -> None:
        async with self.store.transaction() as session:
            asset = await session.get(StudioAsset, asset_id)
            if asset is None or asset.status != "pending":
                return
            run = await session.get(StudioRun, asset.run_id)
            if run is None or run.status != "generating":
                return
            run, plan = await self._approved_plan(session, asset.run_id)
            batch_metadata: dict[str, object] | None = None
            if asset.kind == "voice_batch" and isinstance(plan, ScenePlan):
                profile = self._scene_execution_profile(plan, run)
                if run.mode == "local":
                    archive = getattr(self.voice_provider, "archive", None)
                    if archive is None:
                        raise StudioError("local_media_provider_unavailable", 503)
                    requested_lines = self._narration_lines(plan)
                    source_lines = archive.source_title_lines()
                    source_text = "\n\n".join(source_lines)
                    batch_metadata = {
                        "local_media_receipt": archive.receipt(),
                        "requested_title_lines": list(requested_lines),
                        "requested_title_lines_sha256": sha256(
                            "\n".join(requested_lines).encode()
                        ).hexdigest(),
                        "source_title_lines": list(source_lines),
                        "source_title_text_sha256": sha256(source_text.encode()).hexdigest(),
                        "alignment_status": "ordinal_only_mismatch",
                        "voice_profile": profile["voice_profile"],
                        "voice_profile_sha256": profile["voice_profile_sha256"],
                    }
                else:
                    submitted_text = build_scene_narration_text(
                        self._narration_lines(plan),
                        delivery_cue=str(profile["voice_profile"]["delivery_prefix"]),
                        between_cues=str(profile["voice_profile"]["between_cues"]),
                    )
                    batch_metadata = {
                        "voice_profile": profile["voice_profile"],
                        "voice_profile_sha256": profile["voice_profile_sha256"],
                        "submitted_text": submitted_text,
                        "submitted_text_sha256": sha256(submitted_text.encode()).hexdigest(),
                    }
            asset.status = "submitting"
            asset.submitted_at = _utc_now()
            if batch_metadata is not None:
                asset.media_metadata = batch_metadata
        try:
            if asset.kind == "video":
                idea = plan.ideas[asset.ordinal - 1]
                receipt = await self.video_provider.submit(
                    prompt=idea.visual_prompt,
                    ordinal=asset.ordinal,
                    operation_key=asset.operation_key,
                )
            elif asset.kind == "voice_batch":
                lines = self._narration_lines(plan)
                if isinstance(plan, ScenePlan):
                    receipt = await self.voice_provider.submit_batch(
                        lines=lines,
                        operation_key=asset.operation_key,
                        voice_profile=self._scene_execution_profile(plan, run)["voice_profile"],
                    )
                else:
                    receipt = await self.voice_provider.submit_batch(
                        lines=lines, operation_key=asset.operation_key
                    )
            else:
                return
        except SubmissionUnknown as exc:
            await self._record_unknown(asset_id, getattr(exc, "request_id", None))
            return
        except SubmitRejected:
            await self._record_failed_submission(asset_id)
            return
        except Exception:  # noqa: BLE001 - external adapter or media failure must fail closed
            await self._record_unknown(asset_id, None)
            return
        async with self.store.transaction() as session:
            saved = await session.get(StudioAsset, asset_id)
            if (
                saved
                and saved.operation_key == asset.operation_key
                and saved.status in {"submitting", "submission_unknown"}
            ):
                saved.status, saved.request_id, saved.model, saved.error_code = (
                    "queued",
                    receipt.request_id,
                    receipt.model,
                    None,
                )

    async def _record_unknown(self, asset_id: UUID, request_id: str | None) -> None:
        async with self.store.transaction() as session:
            asset = await session.get(StudioAsset, asset_id)
            if asset is not None and asset.status == "submitting":
                asset.status, asset.request_id, asset.error_code = (
                    "submission_unknown",
                    request_id,
                    "submission_unknown",
                )

    async def _record_failed_submission(self, asset_id: UUID) -> None:
        async with self.store.transaction() as session:
            asset = await session.get(StudioAsset, asset_id)
            if asset is not None and asset.status == "submitting":
                asset.status, asset.error_code, asset.completed_at = (
                    "failed",
                    "submission_rejected",
                    _utc_now(),
                )

    async def reconcile(self, run_id: UUID) -> None:
        """Observe known remote work, collect ready outputs, and make bounded retries."""
        async with self.store.factory() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.status != "generating":
                return
            rows = (
                await session.scalars(
                    select(StudioAsset).where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == run.revision,
                        StudioAsset.request_id.is_not(None),
                        StudioAsset.status.in_(
                            ("queued", "running", "gathering", "unknown", "submission_unknown")
                        ),
                    )
                )
            ).all()
        results = await asyncio.gather(*(self._poll_one(row.id) for row in rows))
        # Fetching is sequential because the byte cap is aggregate and must remain exact.
        for asset_id, result in results:
            if result is not None and result.state == "ready" and result.url:
                async with self.store.factory() as session:
                    asset = await session.get(StudioAsset, asset_id)
                    kind = asset.kind if asset else ""
                    run = await session.get(StudioRun, asset.run_id) if asset else None
                if run is not None and run.status == "generating":
                    async with self.store.transaction() as session:
                        current = await session.get(StudioRun, run.run_id)
                        if (
                            current is not None
                            and current.status == "generating"
                            and current.revision == asset.revision
                        ):
                            seen = await session.scalar(
                                select(StudioEvent.id).where(
                                    StudioEvent.run_id == current.run_id,
                                    StudioEvent.event_type == "workflow_phase",
                                    StudioEvent.detail["revision"].as_integer() == current.revision,
                                    StudioEvent.detail["role"].as_string() == "media_gathering",
                                )
                            )
                            if seen is None:
                                await self.store.event(
                                    session,
                                    current,
                                    "generating",
                                    "workflow_phase",
                                    {
                                        "revision": current.revision,
                                        "component": "worker",
                                        "role": "media_gathering",
                                        "state": "running",
                                    },
                                )
                if kind == "voice_batch":
                    await self._gather_batch(asset_id, result)
                else:
                    await self._gather_one(asset_id, result.url)
        await self._retry_definitive_failures(run_id)
        await self._finish_if_complete(run_id)

    async def _poll_one(self, asset_id: UUID) -> tuple[UUID, AssetResult | None]:
        async with self.store.factory() as session:
            asset = await session.get(StudioAsset, asset_id)
            if asset is None or not asset.request_id:
                return asset_id, None
            provider = self.video_provider if asset.kind == "video" else self.voice_provider
            request_id = asset.request_id
        try:
            result = await provider.poll(request_id)
        except Exception:  # noqa: BLE001 - external adapter or media failure must fail closed
            return asset_id, None
        async with self.store.transaction() as session:
            asset = await session.get(StudioAsset, asset_id)
            if asset is None or asset.request_id != request_id:
                return asset_id, None
            if result.state in {"queued", "running"}:
                asset.status, asset.queue_position = result.state, result.queue_position
            elif result.state == "failed":
                asset.status, asset.error_code, asset.completed_at = (
                    "failed",
                    "provider_failed",
                    _utc_now(),
                )
            elif result.state == "unknown":
                asset.status, asset.error_code = "unknown", "provider_unknown"
        return asset_id, result

    async def _gather_one(self, asset_id: UUID, url: str) -> None:
        async with self.store.factory() as session:
            asset = await session.get(StudioAsset, asset_id)
            if asset is None or asset.status not in {
                "queued",
                "running",
                "gathering",
                "unknown",
                "submission_unknown",
            }:
                return
            used = await session.scalar(
                select(
                    func.coalesce(
                        func.sum(StudioAsset.media_metadata["byte_size"].as_integer()),
                        0,
                    )
                ).where(StudioAsset.run_id == asset.run_id)
            )
        try:
            verified = await fetch_verified_media(
                url=url,
                run_id=str(asset.run_id),
                target_root=self.store.config.media_root,
                policy=self.policy,
                existing_run_bytes=int(used or 0),
                kind="video" if asset.kind == "video" else "audio",
            )
            async with self.store.factory() as approval_session:
                run, plan = await self._approved_plan(approval_session, asset.run_id)
                idea = plan.ideas[asset.ordinal - 1]
            source_metadata = dict(asset.media_metadata or {})
            if run.mode == "local" and (
                source_metadata.get("alignment_status") != "ordinal_only_mismatch"
                or source_metadata.get("source_sha256") != verified.sha256
                or source_metadata.get("requested_prompt_sha256") != asset.input_hash
                or not isinstance(source_metadata.get("local_media_receipt"), dict)
            ):
                raise MediaVerificationError("local_scene_video_provenance_missing")
            if asset.kind == "video":
                verdict = await self.moderator.moderate_images(
                    verified.frame_paths, stage="generated_video"
                )
                coverage = list(verdict.coverage)
            else:
                # This verifies the exact visitor-approved label.  Decode proves
                # technical audio validity; it does not pretend to transcribe it.
                narration = (
                    idea.spoken_text
                    if isinstance(plan, MonthlyPlan)
                    else self._narration_lines(plan)[asset.ordinal - 1]
                )
                verdict = await self.moderator.moderate_text(narration, stage="approved_narration")
                coverage = [
                    *verdict.coverage,
                    f"text_sha256:{sha256(narration.encode()).hexdigest()}",
                ]
            if not verdict.safe:
                if verdict.reason == "moderation unavailable":
                    raise MediaVerificationError("moderation_unavailable")
                raise MediaVerificationError("moderation_rejected")
        except MediaVerificationError as exc:
            if str(exc) == "moderation_rejected":
                await self._record_safety_rejection(asset_id)
            elif str(exc) == "moderation_unavailable":
                await self._record_terminal_validation_failure(asset_id, "moderation_unavailable")
            else:
                await self._record_gather_recovery(asset_id)
            return
        except Exception:  # noqa: BLE001 - local retrieval failure must fail closed
            await self._record_gather_recovery(asset_id)
            return

        async with self.store.transaction() as session:
            current = await session.get(StudioAsset, asset_id)
            if current is None or current.status == "ready":
                return
            await self._approved_plan(session, current.run_id)
            artifact = Artifact(
                run_id=current.run_id,
                persona="producer",
                artifact_type="video_clip" if current.kind == "video" else "voice_clip",
                uri=str(verified.path),
                artifact_metadata={
                    "sha256": verified.sha256,
                    "byte_size": verified.byte_size,
                    "duration_seconds": verified.duration_seconds,
                    "width": verified.width,
                    "height": verified.height,
                },
            )
            session.add(artifact)
            await session.flush()
            (
                current.status,
                current.artifact_id,
                current.sha256,
                current.completed_at,
            ) = ("ready", artifact.id, verified.sha256, _utc_now())
            current.media_metadata = {
                **dict(current.media_metadata or {}),
                "byte_size": verified.byte_size,
                "duration_seconds": verified.duration_seconds,
                "width": verified.width,
                "height": verified.height,
            }
            current.safety = {
                "safe": verdict.safe,
                "reason": verdict.reason,
                "model": verdict.model,
                "coverage": coverage,
                "media_sha256": verified.sha256,
            }

    async def _record_gather_recovery(self, asset_id: UUID) -> None:
        """Keep a known accepted receipt pollable after local retrieval failure."""
        async with self.store.transaction() as session:
            current = await session.get(StudioAsset, asset_id)
            if current and current.status not in {"ready", "gathered"}:
                metadata = dict(current.media_metadata or {})
                attempts = int(metadata.get("retrieval_attempts", 0)) + 1
                metadata["retrieval_attempts"] = attempts
                current.media_metadata = metadata
                if attempts >= 3:
                    current.status, current.error_code, current.completed_at = (
                        "failed",
                        "media_retrieval_exhausted",
                        _utc_now(),
                    )
                else:
                    current.status, current.error_code, current.completed_at = (
                        "gathering",
                        "media_retrieval_failed",
                        None,
                    )

    async def _record_safety_rejection(self, asset_id: UUID) -> None:
        """Stop an unsafe accepted output without treating it as a provider retry."""
        async with self.store.transaction() as session:
            current = await session.get(StudioAsset, asset_id)
            if current and current.status not in {"ready", "gathered"}:
                current.status, current.error_code, current.completed_at = (
                    "failed",
                    "media_safety_rejected",
                    _utc_now(),
                )

    async def _record_terminal_validation_failure(self, asset_id: UUID, error_code: str) -> None:
        async with self.store.transaction() as session:
            current = await session.get(StudioAsset, asset_id)
            if current and current.status not in {"ready", "gathered"}:
                current.status, current.error_code, current.completed_at = (
                    "failed",
                    error_code,
                    _utc_now(),
                )

    async def _checkpoint_preserved_batch_receipt(
        self,
        *,
        asset_id: UUID,
        revision: int,
        timestamps: list[dict[str, Any]],
        verified: Any | None = None,
    ) -> bool:
        """Persist a private live batch receipt before timing validation can reject it."""
        async with self.store.transaction() as session:
            current = await session.get(StudioAsset, asset_id)
            if current is None or current.status == "ready" or current.revision != revision:
                return False
            run, _plan = await self._approved_plan(session, current.run_id)
            if run.revision != revision or current.status not in {
                "queued",
                "running",
                "unknown",
                "submission_unknown",
            }:
                return False
            metadata = dict(current.media_metadata or {})
            metadata.update(
                {
                    "provider_timestamps": timestamps,
                    "timestamps_verified": False,
                }
            )
            if verified is not None:
                metadata.update(
                    {
                        "received_audio_sha256": verified.sha256,
                        "received_audio_byte_size": verified.byte_size,
                        "received_audio_duration_seconds": verified.duration_seconds,
                    }
                )
            current.media_metadata = metadata
            return True

    async def _gather_batch(self, asset_id: UUID, result: Any) -> None:
        """Verify one voice receipt, then derive twelve bounded local WAV assets."""
        from myloware.studio.voice_batch import normalize_fal_timestamps, split_wav
        from myloware.studio.voice_pauses import detect_silences, plan_pause_splits

        async with self.store.factory() as session:
            batch = await session.get(StudioAsset, asset_id)
            if (
                batch is None
                or batch.kind != "voice_batch"
                or batch.status not in {"queued", "running", "unknown", "submission_unknown"}
            ):
                return
            run, plan = await self._approved_plan(session, batch.run_id)
            used = await session.scalar(
                select(
                    func.coalesce(func.sum(StudioAsset.media_metadata["byte_size"].as_integer()), 0)
                ).where(StudioAsset.run_id == batch.run_id)
            )
        async with self.store.transaction() as session:
            current_run = await session.get(StudioRun, batch.run_id)
            seen = await session.scalar(
                select(StudioEvent.id).where(
                    StudioEvent.run_id == batch.run_id,
                    StudioEvent.event_type == "workflow_phase",
                    StudioEvent.detail["revision"].as_integer() == batch.revision,
                    StudioEvent.detail["role"].as_string() == "narration_assembly",
                )
            )
            if (
                current_run is not None
                and current_run.status == "generating"
                and current_run.revision == batch.revision
                and seen is None
            ):
                await self.store.event(
                    session,
                    current_run,
                    "generating",
                    "workflow_phase",
                    {
                        "revision": batch.revision,
                        "component": "worker",
                        "role": "narration_assembly",
                        "state": "running",
                    },
                )
        try:
            lines = self._narration_lines(plan)
            timestamps = tuple(getattr(result, "timestamps", ()))
            submitted_text = build_narration_text(lines) if isinstance(plan, MonthlyPlan) else ""
            if isinstance(plan, ScenePlan):
                profile = self._scene_execution_profile(plan, run)
                metadata = batch.media_metadata or {}
                expected_submitted_text = build_scene_narration_text(
                    lines,
                    delivery_cue=str(profile["voice_profile"]["delivery_prefix"]),
                    between_cues=str(profile["voice_profile"]["between_cues"]),
                )
                expected_model = (
                    FAKE_NARRATION_BATCH_MODEL
                    if run.mode == "fixture"
                    else (
                        LOCAL_SCENE_MEDIA_MODEL
                        if run.mode == "local"
                        else profile["voice_profile"]["model"]
                    )
                )
                if run.mode == "local":
                    source_lines = tuple(metadata.get("source_title_lines", ()))
                    if (
                        metadata.get("alignment_status") != "ordinal_only_mismatch"
                        or not all(isinstance(line, str) and line for line in source_lines)
                        or len(source_lines) != 12
                        or metadata.get("source_title_text_sha256")
                        != sha256("\n\n".join(source_lines).encode()).hexdigest()
                        or batch.input_hash != sha256("\n".join(lines).encode()).hexdigest()
                        or batch.model != expected_model
                        or metadata.get("voice_profile") != profile["voice_profile"]
                        or metadata.get("voice_profile_sha256") != profile["voice_profile_sha256"]
                        or not isinstance(metadata.get("local_media_receipt"), dict)
                    ):
                        raise MediaVerificationError("local_scene_narration_provenance_missing")
                    submitted_text = getattr(result, "input_text", None)
                    if submitted_text != "\n\n".join(source_lines):
                        raise MediaVerificationError("local_scene_source_text_mismatch")
                    split_lines = source_lines
                elif (
                    metadata.get("voice_profile") != profile["voice_profile"]
                    or metadata.get("voice_profile_sha256") != profile["voice_profile_sha256"]
                    or not isinstance(metadata.get("submitted_text"), str)
                    or metadata.get("submitted_text_sha256")
                    != sha256(metadata["submitted_text"].encode()).hexdigest()
                    or metadata["submitted_text"] != expected_submitted_text
                    or batch.input_hash != sha256("\n".join(lines).encode()).hexdigest()
                    or batch.model != expected_model
                ):
                    raise MediaVerificationError("scene_narration_provenance_missing")
                else:
                    submitted_text = metadata["submitted_text"]
                if run.mode != "local":
                    split_lines = lines
            elif run.mode == "recorded":
                submitted_text = getattr(result, "input_text", None)
                if not isinstance(submitted_text, str) or not submitted_text:
                    raise MediaVerificationError("recorded_narration_provenance_missing")
                split_lines = lines
            else:
                split_lines = lines
            if self.store.config.preserve_run_artifacts:
                preserved_timestamps = _json_timestamp_receipt(timestamps)
                if not await self._checkpoint_preserved_batch_receipt(
                    asset_id=asset_id,
                    revision=batch.revision,
                    timestamps=preserved_timestamps,
                ):
                    return
                verified = await fetch_verified_media(
                    url=result.url,
                    run_id=str(run.run_id),
                    target_root=self.store.config.media_root,
                    policy=self.policy,
                    existing_run_bytes=int(used or 0),
                    kind="audio",
                )
                if not await self._checkpoint_preserved_batch_receipt(
                    asset_id=asset_id,
                    revision=batch.revision,
                    timestamps=preserved_timestamps,
                    verified=verified,
                ):
                    return
            else:
                verified = await fetch_verified_media(
                    url=result.url,
                    run_id=str(run.run_id),
                    target_root=self.store.config.media_root,
                    policy=self.policy,
                    existing_run_bytes=int(used or 0),
                    kind="audio",
                )
            words = normalize_fal_timestamps(timestamps, submitted_text)
            if run.mode == "local":
                local_media = getattr(result, "local_media", None)
                if (
                    not isinstance(local_media, dict)
                    or local_media.get("archive") != metadata.get("local_media_receipt")
                    or local_media.get("source_batch_sha256") != verified.sha256
                ):
                    raise MediaVerificationError("local_scene_batch_hash_mismatch")
            silences = await detect_silences(verified.path, verified.duration_seconds)
            splits = plan_pause_splits(split_lines, words, verified.duration_seconds, silences)
            output_dir = verified.path.parent / f"{verified.sha256}-splits"
            paths = await split_wav(verified.path, splits, output_dir)
            voices = [await verify_media_file(path, kind="audio") for path in paths]
            if len(voices) != 12:
                raise ValueError("batch split did not produce twelve voice assets")
            if (
                int(used or 0) + verified.byte_size + sum(voice.byte_size for voice in voices)
                > self.policy.run_byte_cap
            ):
                raise MediaVerificationError("media byte cap exceeded after narration split")
            verdicts = [
                await self.moderator.moderate_text(line, stage="approved_narration")
                for line in split_lines
            ]
            if not all(verdict.safe for verdict in verdicts):
                raise MediaVerificationError("moderation_rejected")
        except Exception:  # noqa: BLE001 - external adapter or media failure must fail closed
            async with self.store.transaction() as session:
                current = await session.get(StudioAsset, asset_id)
                if current and current.status != "ready":
                    current.status, current.error_code, current.completed_at = (
                        "failed",
                        "voice_batch_validation_failed",
                        _utc_now(),
                    )
            return
        async with self.store.transaction() as session:
            current = await session.get(StudioAsset, asset_id)
            if current is None or current.status == "ready":
                return
            # The split happened outside a transaction. Re-establish the exact
            # visitor approval before making any derived output durable.
            await self._approved_plan(session, current.run_id)
            if current.revision != batch.revision or current.status not in {
                "queued",
                "running",
                "unknown",
                "submission_unknown",
            }:
                return
            current.status, current.sha256, current.completed_at = (
                "ready",
                verified.sha256,
                _utc_now(),
            )
            current.media_metadata = {
                **dict(current.media_metadata or {}),
                "byte_size": verified.byte_size,
                "duration_seconds": verified.duration_seconds,
                "split_count": 12,
                "timestamps_verified": True,
                "submitted_text_sha256": sha256(submitted_text.encode()).hexdigest(),
                "mode": run.mode,
                **(
                    {
                        "source_batch_sha256": verified.sha256,
                        "timestamps_verified": True,
                    }
                    if run.mode == "local"
                    else {}
                ),
            }
            current.safety = {
                "safe": True,
                "media_sha256": verified.sha256,
                "coverage": ["timestamps_exact", "measured_silence_splits"],
            }
            for ordinal, (voice, verdict, line) in enumerate(
                zip(voices, verdicts, split_lines, strict=True), 1
            ):
                operation_key = self._operation_key(
                    current.run_id, current.revision, ordinal, "voice", current.attempt
                )
                existing = await session.scalar(
                    select(StudioAsset).where(StudioAsset.operation_key == operation_key)
                )
                if existing:
                    continue
                artifact = Artifact(
                    run_id=current.run_id,
                    persona="producer",
                    artifact_type="voice_clip",
                    uri=str(voice.path),
                    artifact_metadata={
                        "sha256": voice.sha256,
                        "byte_size": voice.byte_size,
                        "duration_seconds": voice.duration_seconds,
                        "recorded_from_batch": verified.sha256,
                    },
                )
                session.add(artifact)
                await session.flush()
                session.add(
                    StudioAsset(
                        run_id=current.run_id,
                        revision=current.revision,
                        ordinal=ordinal,
                        kind="voice",
                        attempt=current.attempt,
                        # The durable asset remains selected by the approved
                        # requested title. The saved local source title is
                        # provenance metadata only and must never replace the
                        # review-bound input hash.
                        input_hash=sha256(lines[ordinal - 1].encode()).hexdigest(),
                        operation_key=operation_key,
                        provider="voice_batch_split",
                        model=current.model,
                        request_id=current.request_id,
                        status="ready",
                        artifact_id=artifact.id,
                        sha256=voice.sha256,
                        media_metadata={
                            "byte_size": voice.byte_size,
                            "duration_seconds": voice.duration_seconds,
                            "width": 0,
                            "height": 0,
                            "batch_sha256": verified.sha256,
                            **(
                                {
                                    "requested_title": lines[ordinal - 1],
                                    "requested_title_sha256": sha256(
                                        lines[ordinal - 1].encode()
                                    ).hexdigest(),
                                    "source_title": line,
                                    "source_title_sha256": sha256(line.encode()).hexdigest(),
                                    "alignment_status": "ordinal_only_mismatch",
                                    "local_media_receipt": metadata["local_media_receipt"],
                                }
                                if run.mode == "local"
                                else {}
                            ),
                        },
                        safety={
                            "safe": True,
                            "reason": verdict.reason,
                            "model": verdict.model,
                            "coverage": [
                                *verdict.coverage,
                                f"text_sha256:{sha256(line.encode()).hexdigest()}",
                                "batch_split",
                            ],
                            "media_sha256": voice.sha256,
                        },
                        completed_at=_utc_now(),
                    )
                )

    async def _retry_definitive_failures(self, run_id: UUID) -> None:
        created_retry = False
        async with self.store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            if run is None:
                return
            failures = (
                await session.scalars(
                    select(StudioAsset).where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == run.revision,
                        StudioAsset.status == "failed",
                    )
                )
            ).all()
            for old in failures:
                if old.kind not in {"video", "voice_batch"} or old.error_code not in {
                    "provider_failed",
                    "submission_rejected",
                }:
                    # Local collection, safety, alignment, and ambiguous receipt
                    # failures retain the existing receipt or stop for review.
                    continue
                if old.attempt > self.store.config.asset_retries:
                    continue
                present = await session.scalar(
                    select(StudioAsset.id).where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == old.revision,
                        StudioAsset.ordinal == old.ordinal,
                        StudioAsset.kind == old.kind,
                        StudioAsset.attempt == old.attempt + 1,
                    )
                )
                if present:
                    continue
                if run.mode == "live" and not await self._retry_is_reserved(session, old):
                    # A retry can become paid work even when the prior provider
                    # result was terminal. Do not create an unreserved attempt.
                    old.error_code = "retry_reservation_missing"
                    continue
                session.add(
                    StudioAsset(
                        run_id=run_id,
                        revision=old.revision,
                        ordinal=old.ordinal,
                        kind=old.kind,
                        attempt=old.attempt + 1,
                        input_hash=old.input_hash,
                        operation_key=self._operation_key(
                            run_id, old.revision, old.ordinal, old.kind, old.attempt + 1
                        ),
                        provider=old.provider,
                        status="pending",
                    )
                )
                created_retry = True
        if created_retry:
            await self.submit(run_id)

    async def _retry_is_reserved(self, session: Any, asset: StudioAsset) -> bool:
        """Require a matching paid-attempt ledger row before a live retry."""
        reservation = await session.get(StudioReservation, asset.run_id)
        if (
            reservation is None
            or not reservation.cost_profile_version
            or reservation.cost_state != "reserved"
        ):
            return False
        attempt = asset.attempt + 1
        if asset.kind == "video":
            stage, operation_key = "video_request", f"month:{asset.ordinal}:attempt:{attempt}"
        elif asset.kind == "voice_batch":
            stage, operation_key = "narration_batch", f"attempt:{attempt}"
        else:
            return False
        reserved = await session.scalar(
            select(StudioCostLedger.id).where(
                StudioCostLedger.run_id == asset.run_id,
                StudioCostLedger.profile_version == reservation.cost_profile_version,
                StudioCostLedger.stage == stage,
                StudioCostLedger.operation_key == operation_key,
                StudioCostLedger.cost_state == "reserved",
                StudioCostLedger.reserved_usd.is_not(None),
                StudioCostLedger.reserved_usd > 0,
            )
        )
        return reserved is not None

    async def _finish_if_complete(self, run_id: UUID) -> None:
        async with self.store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.status != "generating":
                return
            try:
                if not run.plan_hash or parse_plan(run.plan).canonical_sha256 != run.plan_hash:
                    run.status, run.error_code = "blocked", "asset_plan_mismatch"
                    return
            except Exception:  # noqa: BLE001 - external adapter or media failure must fail closed
                run.status, run.error_code = "blocked", "asset_plan_mismatch"
                return
            assets = (
                await session.scalars(
                    select(StudioAsset)
                    .where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == run.revision,
                    )
                    .order_by(
                        StudioAsset.ordinal,
                        StudioAsset.kind,
                        StudioAsset.attempt.desc(),
                    )
                )
            ).all()
            selected: dict[tuple[int, str], StudioAsset] = {}
            for asset in assets:
                selected.setdefault((asset.ordinal, asset.kind), asset)
            required = {(ordinal, kind) for ordinal in range(1, 13) for kind in ("video", "voice")}
            batch = selected.get((0, "voice_batch"))
            if (
                set(selected).intersection(required) != required
                or batch is None
                or batch.status != "ready"
                or any(selected[key].status != "ready" for key in required)
            ):
                exhausted = [
                    a
                    for a in selected.values()
                    if a.status == "failed"
                    and (
                        a.attempt > self.store.config.asset_retries
                        or a.error_code
                        in {
                            "media_safety_rejected",
                            "media_retrieval_exhausted",
                            "moderation_unavailable",
                            "retry_reservation_missing",
                            "voice_batch_validation_failed",
                        }
                    )
                ]
                ambiguous = [
                    a
                    for a in selected.values()
                    if a.status == "submission_unknown" and not a.request_id
                ]
                if exhausted:
                    run.status, run.error_code = "failed", "asset_retries_exhausted"
                    await self.store.event(
                        session,
                        run,
                        "failed",
                        "asset_retries_exhausted",
                        {"count": len(exhausted)},
                    )
                    if run.mode in {"fixture", "recorded"}:
                        await self.store.close_fixture_reservation(session, run_id)
                elif ambiguous:
                    run.status, run.error_code = (
                        "submission_unknown",
                        "submission_unknown",
                    )
                    await self.store.event(
                        session,
                        run,
                        "submission_unknown",
                        "submission_unknown",
                        {"count": len(ambiguous)},
                    )
                return
            for ordinal in range(1, 13):
                video, voice = (
                    selected[(ordinal, "video")],
                    selected[(ordinal, "voice")],
                )
                vm, am = video.media_metadata or {}, voice.media_metadata or {}
                if (
                    int(vm.get("height", 0)) <= int(vm.get("width", 0))
                    or float(am.get("duration_seconds", 0)) > float(vm.get("duration_seconds", 0))
                    or video.safety is None
                    or voice.safety is None
                    or video.safety.get("media_sha256") != video.sha256
                    or voice.safety.get("media_sha256") != voice.sha256
                ):
                    run.status, run.error_code = "blocked", "asset_contract_invalid"
                    return
            run.status = "editing"
            await self.store.event(
                session,
                run,
                "editing",
                "assets_ready",
                {"video_count": 12, "voice_count": 12, "voice_batch_count": 1},
            )

    async def _approved_plan(self, session: Any, run_id: UUID) -> tuple[StudioRun, StudioPlan]:
        run = await session.get(StudioRun, run_id)
        if (
            run is None
            or run.cancelled
            or run.expires_at <= _utc_now()
            or run.status != "generating"
            or not run.plan
            or not run.plan_hash
            or run.plan_hash != run.plan_approved_hash
        ):
            raise StudioError("stale_review")
        approval = await session.scalar(
            select(StudioDecision)
            .where(
                StudioDecision.run_id == run_id,
                StudioDecision.visitor_id == run.visitor_id,
                StudioDecision.gate == "ideas",
                StudioDecision.revision == run.revision,
                StudioDecision.subject_hash == run.plan_hash,
                StudioDecision.decision == "approve",
                StudioDecision.expires_at > _utc_now(),
            )
            .limit(1)
        )
        if approval is None:
            raise StudioError("stale_review")
        plan = parse_plan(run.plan)
        if plan.canonical_sha256 != run.plan_hash:
            raise StudioError("stale_review")
        from myloware.studio.execution_profile import require_plan_execution

        try:
            require_plan_execution(plan, getattr(run, "execution_profile", None))
        except ValueError as exc:
            raise StudioError("plan_execution_version_mismatch") from exc
        return run, plan

    @staticmethod
    def _narration_lines(plan: StudioPlan) -> tuple[str, ...]:
        return (
            canonical_narration_lines(plan)
            if isinstance(plan, MonthlyPlan)
            else scene_narration_lines(plan)
        )

    @staticmethod
    def _scene_execution_profile(plan: ScenePlan, run: StudioRun) -> dict[str, Any]:
        from myloware.studio.execution_profile import require_plan_execution

        profile = require_plan_execution(plan, getattr(run, "execution_profile", None))
        if profile is None:
            raise StudioError("scene_execution_profile_missing")
        return profile

    @staticmethod
    def _operation_key(run_id: UUID, revision: int, ordinal: int, kind: str, attempt: int) -> str:
        return f"monthly:{run_id}:{revision}:{ordinal}:{kind}:{attempt}"
