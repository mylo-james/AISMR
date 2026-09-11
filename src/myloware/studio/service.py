"""Composition of the monthly workflow's existing storage and provider seams."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select

from myloware.config import settings
from myloware.config.studio import get_studio_settings
from myloware.providers.fal_video import FalVideoProvider
from myloware.providers.fal_voice_batch import FalNarrationBatchProvider
from myloware.providers.media import FakeNarrationBatchProvider, FakeVideoProvider
from myloware.storage.database import get_async_session_factory
from myloware.storage.models import Artifact, _utc_now
from myloware.storage.studio_models import StudioAsset, StudioEvent, StudioPlannerRun, StudioRun
from myloware.storage.studio_store import StudioError, StudioStore, digest
from myloware.studio.assets import MonthlyAssets
from myloware.studio.creative_planning import CreativePlanner
from myloware.studio.editor import MonthlyEditor, approved_recorded_edit_plan
from myloware.studio.execution_profile import require_plan_execution
from myloware.studio.fixtures import fixture_batch_timestamps, prepare_fixture_media
from myloware.studio.ideation import MonthlyIdeator
from myloware.studio.media import MediaFetchPolicy, MediaVerificationError, fetch_verified_media
from myloware.studio.moderation import Moderator, build_moderator
from myloware.workflows.monthly import deterministic_fixture_plan
from myloware.workflows.scenes import ScenePlan, deterministic_scene_plan, parse_plan


class StudioService:
    def __init__(
        self,
        store: StudioStore,
        *,
        moderator: Moderator,
        ideator: MonthlyIdeator | None = None,
        creative_planner: CreativePlanner | None = None,
    ):
        self.store = store
        self.config = store.config
        self.moderator = moderator
        self.ideator = ideator
        self.creative_planner = creative_planner
        self._closeables: list[object] = []

    def _track(self, value: object) -> object:
        self._closeables.append(value)
        return value

    async def aclose(self) -> None:
        """Close clients created for this graph stage without retaining a global cache."""
        closed: set[int] = set()
        while self._closeables:
            value = self._closeables.pop()
            if id(value) in closed:
                continue
            closed.add(id(value))
            close = getattr(value, "aclose", None)
            if callable(close):
                await close()
        if self.ideator is not None:
            client = getattr(self.ideator, "_client", None)
            if id(client) in closed:
                return
            close = getattr(client, "aclose", None)
            if callable(close):
                await close()

    def internal_url(self, run_id: UUID, path: str) -> str:
        token = self.store.sign("media", str(run_id))
        return f"{self.config.origin}/v1/studio/internal/{run_id}/{token}/{path}"

    async def observed(
        self, run_id: UUID, event_type: str, detail: dict[str, Any] | None = None
    ) -> None:
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            if current is not None:
                await self.store.event(session, current, current.status, event_type, detail)

    async def _require_pinned_local_media_receipt(self, run: StudioRun) -> None:
        """Fail closed if a local replay archive changed after admission."""
        if run.mode != "local":
            return
        if self.config.local_media_root is None:
            raise StudioError("local_media_receipt_mismatch", 409)
        from myloware.studio.local_scene_media import LocalSceneMediaArchive

        archive = LocalSceneMediaArchive(self.config.local_media_root)
        async with self.store.factory() as session:
            planner = await session.get(StudioPlannerRun, run.run_id)
        if planner is None or planner.configuration.get("local_media_receipt") != archive.receipt():
            raise StudioError("local_media_receipt_mismatch", 409)

    def media_policy(self, *, renderer: bool = False) -> MediaFetchPolicy:
        origins = self.config.media_allowed_origins
        prefixes = self.config.media_allowed_path_prefixes
        if self.config.mode == "local" and not renderer:
            origins = [self.config.origin]
            prefixes = []
        elif self.config.mode in {"fixture", "recorded"}:
            origins = [*origins, self.config.origin]
        if renderer:
            origins = [settings.remotion_service_url.rstrip("/")]
        return MediaFetchPolicy(
            allowed_origins=frozenset(origins),
            # A full edit contains twelve clips and may exceed the individual
            # input limit, but must still fit the existing total run limit.
            asset_byte_cap=self.config.max_run_bytes if renderer else self.config.max_asset_bytes,
            run_byte_cap=self.config.max_run_bytes,
            allowed_path_prefixes=frozenset(prefixes),
            # The renderer is a separately configured local trusted service.
            # Provider asset fetches keep the stricter non-loopback policy.
            fixture_mode=self.config.mode in {"fixture", "recorded", "local"} or renderer,
        )

    def assets(self, run_id: UUID, *, run: StudioRun | None = None) -> MonthlyAssets:
        if self.config.mode == "planning" or (run is not None and run.mode == "planning"):
            raise StudioError("planning_only", 409)
        if self.config.mode == "fixture":
            base = self.internal_url(run_id, "fixture")
            scene_mode = (
                bool(run.execution_profile)
                if run is not None
                else (
                    self.config.fixture_root / str(run_id) / "title-batch-timestamps.json"
                ).is_file()
            )
            video = FakeVideoProvider(trusted_fixture_base_url=base, scene_mode=scene_mode)
            voice = FakeNarrationBatchProvider(
                trusted_fixture_base_url=base,
                timestamps=fixture_batch_timestamps(
                    self.config.fixture_root, str(run_id), scene_mode=scene_mode
                ),
                scene_mode=scene_mode,
            )
        elif self.config.mode == "recorded":
            from myloware.providers.media import (
                RecordedNarrationBatchProvider,
                RecordedVideoProvider,
            )
            from myloware.studio.recorded import RecordedMediaArchive

            if self.config.recorded_root is None:
                raise StudioError("recorded_archive_missing")
            archive = RecordedMediaArchive(self.config.recorded_root)
            base = self.internal_url(run_id, "").rstrip("/")
            video = RecordedVideoProvider(base_url=base)
            voice = RecordedNarrationBatchProvider(
                base_url=base,
                timestamps=archive.batch_timestamps(),
                input_text=archive.batch_input_text(),
            )
        elif self.config.mode == "local":
            from myloware.providers.media import (
                LocalSceneNarrationBatchProvider,
                LocalSceneVideoProvider,
            )
            from myloware.studio.local_scene_media import LocalSceneMediaArchive

            if self.config.local_media_root is None:
                raise StudioError("local_media_archive_missing")
            archive = LocalSceneMediaArchive(self.config.local_media_root)
            base = self.internal_url(run_id, "local")
            video = LocalSceneVideoProvider(archive=archive, trusted_local_base_url=base)
            voice = LocalSceneNarrationBatchProvider(archive=archive, trusted_local_base_url=base)
        else:
            video = FalVideoProvider(key=self.config.fal_key.get_secret_value())
            voice = FalNarrationBatchProvider(key=self.config.fal_key.get_secret_value())
        self._track(video)
        self._track(voice)
        return MonthlyAssets(
            self.store,
            video,
            voice,
            self.moderator,
            self.media_policy(),
            renderer_preflight=MonthlyEditor(real_render=self.config.render_real).preflight_scenes,
        )

    async def ideate(self, run_id: UUID) -> None:
        run = await self.store.get_run(run_id)
        if run.status != "ideating" or run.cancelled:
            return
        from myloware.studio.planning_store import planner_version

        if await planner_version(self.store, run_id) == "creative-v2":
            from myloware.studio.planning_service import ideate_creative

            await ideate_creative(self, run)
            return
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            if current is None or current.status != "ideating":
                return
            moderation: dict[str, Any] = dict(current.moderation or {})
            prior = moderation.get("stage_intent")
            if isinstance(prior, dict) and prior.get("revision") == current.revision:
                current.status = "submission_unknown"
                current.error_code = "ideation_submission_unknown"
                await self.store.event(
                    session, current, current.status, "ideation_submission_unknown"
                )
                return
            current.moderation = {
                "stage_intent": {"revision": current.revision, "state": "submitting"}
            }
            await self.store.event(
                session,
                current,
                "ideating",
                "ideation_submitting",
                {"revision": current.revision},
            )
        verdict = await self.moderator.moderate_text(run.item_text, stage="input")
        if not verdict.safe:
            await self.store.stop(run_id, "input_moderation_blocked")
            return
        await self.observed(run_id, "input_moderation_checked")
        if self.config.mode == "fixture":
            plan = (
                deterministic_scene_plan(run_id, run.item_text, run.revision)
                if run.execution_profile
                else deterministic_fixture_plan(run_id, run.item_text, run.revision)
            )
        elif self.config.mode == "recorded":
            from myloware.studio.recorded import RecordedMediaArchive

            if self.config.recorded_root is None:
                raise StudioError("recorded_archive_missing")
            plan = RecordedMediaArchive(self.config.recorded_root).plan_for(
                run_id=run_id, revision=run.revision, item_text=run.item_text
            )
        else:
            if self.ideator is None:
                raise StudioError("ideator_unavailable", 503)
            loaded_sources = [
                source.public() for source in getattr(self.ideator, "knowledge_sources", ())
            ]
            if loaded_sources:
                await self.observed(run_id, "knowledge_loaded", {"sources": loaded_sources})
            plan = await self.ideator.create_plan(
                run_id=run_id, revision=run.revision, item=run.item_text
            )
            submitted_sources = [
                source.public()
                for source in getattr(self.ideator, "last_prompt_context_sources", ())
            ]
            if submitted_sources:
                await self.observed(
                    run_id, "knowledge_context_submitted", {"sources": submitted_sources}
                )
        output = await self.moderator.moderate_text(plan.model_dump_json(), stage="plan")
        if not output.safe:
            await self.store.stop(run_id, "plan_moderation_blocked")
            return
        await self.observed(run_id, "plan_moderation_checked")
        if self.config.mode == "fixture":
            await prepare_fixture_media(plan, self.config.fixture_root)
        await self.store.save_plan(
            plan,
            {
                "input": asdict(verdict),
                "plan": asdict(output),
                "plan_hash": plan.canonical_sha256,
            },
        )

    async def submit_edit(self, run_id: UUID) -> None:
        if self.config.mode == "planning":
            raise StudioError("planning_only", 409)
        run = await self.store.get_run(run_id)
        if run.mode == "planning":
            raise StudioError("planning_only", 409)
        await self._require_pinned_local_media_receipt(run)
        if run.status != "editing" or run.cancelled or run.render_job_id:
            return
        if run.render_submission_state in {"submitting", "unknown"}:
            await self.store.stop(run_id, "render_submission_unknown", status="submission_unknown")
            return
        plan = parse_plan(run.plan)
        execution_profile = require_plan_execution(plan, run.execution_profile)
        if (
            plan.run_id != run_id
            or plan.revision != run.revision
            or plan.canonical_sha256 != run.plan_hash
            or run.plan_hash != run.plan_approved_hash
        ):
            raise StudioError("stale_review")
        async with self.store.factory() as session:
            rows = (
                await session.scalars(
                    select(StudioAsset)
                    .where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == run.revision,
                        StudioAsset.status == "ready",
                        StudioAsset.kind.in_({"video", "voice", "voice_batch"}),
                    )
                    .order_by(StudioAsset.ordinal, StudioAsset.attempt)
                )
            ).all()
        selected = {
            (int(row.ordinal), str(row.kind)): row for row in rows if row.kind != "voice_batch"
        }
        if isinstance(plan, ScenePlan):
            from myloware.studio.scene_media import validate_scene_assets

            if execution_profile is None:
                raise StudioError("scene_execution_profile_missing")
            try:
                selected = validate_scene_assets(plan, execution_profile, rows, mode=run.mode)
            except ValueError as exc:
                raise StudioError(str(exc)) from exc
        if len(selected) != 24 or any(
            (ordinal, kind) not in selected
            for ordinal in range(1, 13)
            for kind in ("video", "voice")
        ):
            raise StudioError("assets_incomplete")
        music_url = None
        music_hash = None
        edit_plan = None
        if self.config.music_id is not None:
            from myloware.studio.music import cleared_music

            music_path = cleared_music(self.config.music_id)
            music_hash = sha256(music_path.read_bytes()).hexdigest()
            music_url = self.internal_url(run_id, f"music/{self.config.music_id}")
        if self.config.mode == "recorded":
            if self.config.recorded_root is None:
                raise StudioError("recorded_archive_missing")
            edit_plan = approved_recorded_edit_plan(self.config.recorded_root)
        elif self.config.mode == "live" or isinstance(plan, ScenePlan):
            from myloware.studio.editor import approved_monthly_edit_plan

            edit_plan = approved_monthly_edit_plan()
        input_hash = digest(
            {
                "plan": plan.canonical_sha256,
                "assets": [
                    selected[(ordinal, kind)].sha256
                    for ordinal in range(1, 13)
                    for kind in ("video", "voice")
                ],
                "music": music_hash,
                "edit_plan": edit_plan,
                **({"execution_profile": execution_profile} if execution_profile else {}),
            }
        )
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            if current.status != "editing" or current.render_submission_state:
                return
            current.render_input_hash = input_hash
            current.render_submission_state = "submitting"
            await self.store.event(session, current, "editing", "render_submitting", {"scenes": 12})
            await self.store.event(
                session,
                current,
                "editing",
                "workflow_phase",
                {
                    "revision": current.revision,
                    "component": "worker",
                    "role": "render_assembly",
                    "state": "validated",
                    "counts": {"scenes": 12},
                },
            )
        callback = f"{self.config.origin}/v1/studio/callbacks/remotion/{run_id}"
        editor = MonthlyEditor(real_render=self.config.render_real)
        common: dict[str, Any] = {
            "run_id": str(run_id),
            "input_hash": input_hash,
            "music_url": music_url,
            "edit_plan": edit_plan,
            "callback_url": callback,
        }
        if isinstance(plan, ScenePlan):
            from myloware.studio.editor import SceneOutput

            if execution_profile is None:
                raise StudioError("scene_execution_profile_missing")
            response = await editor.submit_scenes(
                **common,
                execution_profile=execution_profile,
                scenes=[
                    SceneOutput(
                        ordinal=scene.ordinal,
                        title=scene.title,
                        video_ref=self.internal_url(
                            run_id, f"assets/{selected[(scene.ordinal, 'video')].artifact_id}"
                        ),
                        title_audio_ref=self.internal_url(
                            run_id, f"assets/{selected[(scene.ordinal, 'voice')].artifact_id}"
                        ),
                    )
                    for scene in plan.scenes
                ],
            )
        else:
            response = await editor.submit(
                **common,
                plan=plan,
                video_urls=[
                    self.internal_url(run_id, f"assets/{selected[(ordinal, 'video')].artifact_id}")
                    for ordinal in range(1, 13)
                ],
                narration_urls=[
                    self.internal_url(run_id, f"assets/{selected[(ordinal, 'voice')].artifact_id}")
                    for ordinal in range(1, 13)
                ],
            )
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            if current.render_input_hash != input_hash:
                return
            if response.get("render_job_id") and response.get("status") in {
                "accepted",
                "unknown",
            }:
                current.render_job_id = response["render_job_id"]
                current.render_submission_state = "accepted"
                await self.store.event(session, current, "editing", "render_queued")
            else:
                current.render_submission_state = (
                    "unknown" if response.get("status") == "unknown" else "rejected"
                )
                current.status = (
                    "submission_unknown" if response.get("status") == "unknown" else "failed"
                )
                current.error_code = "render_unavailable"
                await self.store.event(session, current, current.status, "render_unavailable")

    async def _record_render_progress(
        self,
        *,
        run_id: UUID,
        expected_job_id: str,
        expected_input_hash: str,
        status: dict[str, Any],
    ) -> None:
        """Persist a bounded renderer receipt only when it advances visibly."""
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            if (
                current is None
                or current.status != "editing"
                or current.render_job_id != expected_job_id
                or current.render_input_hash != expected_input_hash
            ):
                return
            rows = (
                await session.scalars(
                    select(StudioEvent)
                    .where(
                        StudioEvent.run_id == run_id,
                        StudioEvent.event_type == "render_progress",
                    )
                    .order_by(StudioEvent.sequence.desc())
                )
            ).all()
            previous = next(
                (
                    row.detail
                    for row in rows
                    if isinstance(row.detail, dict)
                    and row.detail.get("revision") == current.revision
                ),
                None,
            )
            phase = status["phase"]
            progress = float(status["progress"])
            bucket = int(progress * 20)
            if isinstance(previous, dict):
                previous_phase = previous.get("phase")
                previous_progress = previous.get("progress")
                phase_order = {
                    "queued": 0,
                    "preparing": 1,
                    "composition": 2,
                    "frames": 3,
                    "encoding": 4,
                    "verification": 5,
                    "complete": 6,
                }
                if previous_phase == "failed" or (
                    phase != "failed"
                    and (
                        not isinstance(previous_progress, (int, float))
                        or progress < float(previous_progress)
                        or (
                            isinstance(previous_phase, str)
                            and previous_phase in phase_order
                            and phase_order.get(phase, -1) < phase_order[previous_phase]
                        )
                    )
                ):
                    return
                if (
                    previous_phase == phase
                    and isinstance(previous_progress, (int, float))
                    and int(float(previous_progress) * 20) == bucket
                ):
                    return
            await self.store.event(
                session,
                current,
                "editing",
                "render_progress",
                {"revision": current.revision, **status},
            )

    async def _record_render_workflow_phase(
        self,
        *,
        run_id: UUID,
        expected_job_id: str,
        expected_input_hash: str,
        role: str,
    ) -> None:
        """Record the start of a concrete deterministic final-render check."""
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            if (
                current is None
                or current.status != "editing"
                or current.render_job_id != expected_job_id
                or current.render_input_hash != expected_input_hash
            ):
                return
            rows = (
                await session.scalars(
                    select(StudioEvent)
                    .where(
                        StudioEvent.run_id == run_id,
                        StudioEvent.event_type == "workflow_phase",
                    )
                    .order_by(StudioEvent.sequence.desc())
                )
            ).all()
            if any(
                isinstance(row.detail, dict)
                and row.detail.get("revision") == current.revision
                and row.detail.get("role") == role
                for row in rows
            ):
                return
            await self.store.event(
                session,
                current,
                "editing",
                "workflow_phase",
                {
                    "revision": current.revision,
                    "component": "check",
                    "role": role,
                    "state": "running",
                },
            )

    async def inspect_edit(self, run_id: UUID) -> None:
        if self.config.mode == "planning":
            raise StudioError("planning_only", 409)
        run = await self.store.get_run(run_id)
        if run.mode == "planning":
            raise StudioError("planning_only", 409)
        await self._require_pinned_local_media_receipt(run)
        if run.status != "editing" or not run.render_job_id:
            return
        if not run.render_input_hash:
            await self.store.stop(run_id, "render_input_hash_missing", status="submission_unknown")
            return
        result = await MonthlyEditor().poll(
            run_id=str(run_id),
            render_job_id=run.render_job_id,
            input_hash=run.render_input_hash,
        )
        poll_status = result.get("status")
        render_status = result.get("render_status")
        if poll_status == "running":
            async with self.store.transaction() as session:
                current = await session.get(StudioRun, run_id)
                if (
                    current
                    and current.status == "editing"
                    and current.render_job_id == run.render_job_id
                    and current.render_input_hash == run.render_input_hash
                    and current.render_submission_state == "accepted"
                ):
                    current.render_submission_state = "running"
                    await self.store.event(session, current, "editing", "render_running")
        if isinstance(render_status, dict) and poll_status in {
            "queued",
            "running",
            "ready",
            "failed",
        }:
            await self._record_render_progress(
                run_id=run_id,
                expected_job_id=run.render_job_id,
                expected_input_hash=run.render_input_hash,
                status=render_status,
            )
        if poll_status == "failed":
            await self.store.stop(run_id, "render_failed", status="failed")
            return
        if poll_status == "rejected":
            await self.store.stop(run_id, "render_rejected", status="failed")
            return
        if poll_status == "unknown":
            # A missing/mismatched receipt or transient renderer outage cannot
            # establish that another submission would be safe.
            await self.store.stop(run_id, "render_receipt_unknown", status="submission_unknown")
            return
        if (
            poll_status != "ready"
            or result.get("media_verified") is not True
            or not result.get("final_url")
        ):
            if poll_status == "ready":
                await self.store.stop(
                    run_id, "render_result_unverified", status="submission_unknown"
                )
            return
        secret = settings.remotion_api_secret
        async with self.store.factory() as session:
            assets = (
                await session.scalars(
                    select(StudioAsset).where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == run.revision,
                    )
                )
            ).all()
        render_provenance = None
        if run.execution_profile is not None:
            from myloware.studio.scene_media import (
                validate_render_provenance,
                validate_scene_assets,
            )

            try:
                plan = parse_plan(run.plan)
                profile = require_plan_execution(plan, run.execution_profile)
                if (
                    not isinstance(plan, ScenePlan)
                    or profile is None
                    or plan.canonical_sha256 != run.plan_approved_hash
                    or plan.canonical_sha256 != run.plan_hash
                ):
                    raise ValueError("scene_render_plan_mismatch")
                selected = validate_scene_assets(plan, profile, assets, mode=run.mode)
                render_provenance = validate_render_provenance(
                    result.get("render_provenance"), profile, selected
                )
            except ValueError as exc:
                raise StudioError(str(exc)) from exc
        await self._record_render_workflow_phase(
            run_id=run_id,
            expected_job_id=run.render_job_id,
            expected_input_hash=run.render_input_hash,
            role="final_media_verification",
        )
        input_bytes = 0
        for asset in assets:
            asset_metadata: dict[str, Any] = dict(asset.media_metadata or {})
            input_bytes += int(asset_metadata.get("byte_size", 0))
        try:
            verified = await fetch_verified_media(
                url=result["final_url"],
                run_id=str(run_id),
                target_root=self.config.media_root,
                policy=self.media_policy(renderer=True),
                existing_run_bytes=input_bytes,
                request_headers={"Authorization": f"Bearer {secret}", "x-api-key": secret},
            )
        except MediaVerificationError as exc:
            # Re-downloading a rejected completed render cannot fix its bytes.
            raise StudioError("final_media_invalid") from exc
        if verified.width != 1080 or verified.height != 1920:
            raise StudioError("final_dimensions_invalid")
        await self._record_render_workflow_phase(
            run_id=run_id,
            expected_job_id=run.render_job_id,
            expected_input_hash=run.render_input_hash,
            role="final_moderation",
        )
        verdict = await self.moderator.moderate_images(verified.frame_paths, stage="final")
        if not verdict.safe:
            await self.store.stop(run_id, "final_moderation_blocked")
            return
        await self.observed(run_id, "final_moderation_checked")
        final_path = self.config.media_root / str(run_id) / "final.mp4"
        verified.path.replace(final_path)
        from myloware.studio.music import cleared_music
        from myloware.studio.public_rights import public_rights_receipt, public_suitability_receipt

        music_sha256 = None
        if self.config.music_id is not None:
            try:
                music_sha256 = sha256(cleared_music(self.config.music_id).read_bytes()).hexdigest()
            except (OSError, StudioError):
                # A missing local receipt does not invalidate the private final,
                # but it cannot produce public-use eligibility.
                music_sha256 = None
        moderation = asdict(verdict)
        public_suitability = public_suitability_receipt(
            final_hash=verified.sha256, moderation=moderation
        )
        public_rights = public_rights_receipt(
            profile_path=self.config.rights_profile_path,
            final_hash=verified.sha256,
            assets=assets,
            music_id=self.config.music_id,
            music_sha256=music_sha256,
            render_provenance=render_provenance,
        )
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            if (
                current.status != "editing"
                or current.render_job_id != run.render_job_id
                or current.render_input_hash != run.render_input_hash
            ):
                return
            metadata = {
                "path": str(final_path.resolve()),
                "media_verified": True,
                "duration_seconds": verified.duration_seconds,
                "width": verified.width,
                "height": verified.height,
                "byte_size": verified.byte_size,
                "sha256": verified.sha256,
                "render_input_hash": current.render_input_hash,
                "moderation": moderation,
                "public_suitability": public_suitability,
                "public_rights": public_rights,
                "fixture": run.mode == "fixture",
                "recorded": run.mode == "recorded",
                **({"render_provenance": render_provenance} if render_provenance else {}),
            }
            artifact = Artifact(
                run_id=run_id,
                persona="editor",
                artifact_type="rendered_video",
                uri=str(final_path.resolve()),
                artifact_metadata=metadata,
            )
            session.add(artifact)
            await session.flush()
            current.final_artifact_id = artifact.id
            current.final_hash = verified.sha256
            current.final_metadata = metadata
            current.status = "final_review"
            current.final_review_created_at = _utc_now()
            current.final_review_expires_at = current.final_review_created_at + timedelta(
                hours=self.config.final_review_hours
            )
            await self.store.event(
                session,
                current,
                "final_review",
                "render_verified",
                {
                    "duration_seconds": round(verified.duration_seconds, 2),
                    "width": verified.width,
                    "height": verified.height,
                },
            )


def build_studio_service() -> StudioService:
    config = get_studio_settings()
    store = StudioStore(get_async_session_factory(), config)
    if not config.enabled:
        raise StudioError("studio_disabled", 503)
    if config.mode in {"fixture", "recorded"}:
        moderator = build_moderator(mode="fixture", fixture_outcome=config.fixture_moderation)
        if config.recorded_final_reuse:
            from myloware.studio.hosted_recorded import HostedRecordedService

            return HostedRecordedService(store, moderator=moderator)
        return StudioService(store, moderator=moderator)
    from myloware.studio.runtime import (
        build_live_components,
        build_local_components,
        build_planning_components,
    )

    components = (
        build_planning_components(config)
        if config.mode == "planning"
        else (
            build_local_components(config)
            if config.mode == "local"
            else build_live_components(config)
        )
    )
    service = StudioService(
        store,
        moderator=components.moderator,
        ideator=components.ideator,
        creative_planner=components.creative_planner,
    )
    for client in components.closeables:
        service._track(client)
    return service


@asynccontextmanager
async def open_studio_service(
    builder: Callable[[], StudioService] = build_studio_service,
) -> AsyncIterator[StudioService]:
    """Own one composition root and all provider clients for one graph stage."""
    service = builder()
    try:
        yield service
    finally:
        await service.aclose()
