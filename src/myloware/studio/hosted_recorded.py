"""Immutable recorded inputs for the existing workflow, without provider effects."""

from __future__ import annotations

import json
from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select

from myloware.config.studio import StudioSettings
from myloware.storage.models import Artifact, _utc_now
from myloware.storage.studio_models import StudioAsset, StudioPlannerRun, StudioRun
from myloware.storage.studio_store import StudioError, digest
from myloware.studio.service import StudioService


def recorded_bundle(config: StudioSettings) -> dict[str, Any]:
    if not config.recorded_final_reuse or config.recorded_root is None:
        raise StudioError("recorded_reuse_disabled", 503)
    raw = (config.recorded_root / "fixture-manifest.json").read_bytes()
    if sha256(raw).hexdigest() != config.recorded_bundle_sha256:
        raise StudioError("recorded_bundle_changed", 503)
    bundle = json.loads(raw)
    if bundle.get("schema_version") != 1:
        raise StudioError("recorded_bundle_invalid", 503)
    return bundle


def bundle_entry(config: StudioSettings, role: str, ordinal: int | None = None) -> dict[str, Any]:
    entries = [
        entry
        for entry in recorded_bundle(config)["entries"]
        if entry["role"] == role and entry.get("ordinal") == ordinal
    ]
    if len(entries) != 1:
        raise StudioError("recorded_bundle_invalid", 503)
    return entries[0]


def object_uri(config: StudioSettings, entry: dict[str, Any]) -> str:
    return f"s3://{config.recorded_bucket}/{entry['object_key']}"


async def recorded_preview_url(config: StudioSettings, final_hash: str, etag: str) -> str:
    from botocore.exceptions import BotoCoreError, ClientError

    from myloware.storage.object_store import get_s3_store

    final = bundle_entry(config, "approved_local_render")
    if final["sha256"] != final_hash:
        raise StudioError("recorded_bundle_changed", 409)
    if not etag:
        raise StudioError("preview_unavailable", 404)
    try:
        await get_s3_store().require_object_etag_async(
            uri=object_uri(config, final), expected_etag=etag
        )
        return await get_s3_store().presign_get_async(
            uri=object_uri(config, final), expires_seconds=300
        )
    except (ValueError, BotoCoreError, ClientError) as exc:
        raise StudioError("preview_unavailable", 404) from exc


class HostedRecordedAssets:
    def __init__(self, service: HostedRecordedService):
        self.service = service
        self.store = service.store

    async def submit(self, run_id: UUID) -> None:
        run = await self.service.checked_run(run_id)
        if run.status != "generating":
            return
        if not run.plan_hash or run.plan_approved_hash != run.plan_hash:
            raise StudioError("plan_not_approved", 409)
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            if current.status != "generating":
                return
            existing = await session.scalar(
                select(StudioAsset.id).where(StudioAsset.run_id == run_id)
            )
            if existing is not None:
                return
            for ordinal in range(1, 13):
                for kind, role in (
                    ("video", "recorded_video_provider_output"),
                    ("voice", "recorded_voice_provider_output_pause_split"),
                ):
                    entry = bundle_entry(self.store.config, role, ordinal)
                    artifact = Artifact(
                        run_id=run_id,
                        persona="recorded_archive",
                        artifact_type=f"recorded_{kind}",
                        uri=object_uri(self.store.config, entry),
                        artifact_metadata={
                            "sha256": entry["sha256"],
                            "byte_size": entry["byte_size"],
                            "recorded": True,
                        },
                    )
                    session.add(artifact)
                    await session.flush()
                    session.add(
                        StudioAsset(
                            run_id=run_id,
                            revision=current.revision,
                            ordinal=ordinal,
                            kind=kind,
                            input_hash=current.plan_hash,
                            operation_key=f"recorded:{run_id}:{ordinal}:{kind}",
                            provider="recorded_archive",
                            model="stored-teacup-v3",
                            status="ready",
                            artifact_id=artifact.id,
                            sha256=entry["sha256"],
                            media_metadata={"byte_size": entry["byte_size"], "recorded": True},
                            safety={"source": "recorded_sample", "fresh_check": False},
                            completed_at=_utc_now(),
                        )
                    )
            await self.store.event(
                session,
                current,
                "generating",
                "recorded_assets_loaded",
                {"videos": 12, "scenes": 12},
            )

    async def reconcile(self, run_id: UUID) -> None:
        run = await self.service.checked_run(run_id)
        if run.status != "generating":
            return
        await self.service.require_assets(run_id)
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            if current.status == "generating":
                current.status = "editing"
                await self.store.event(session, current, "editing", "recorded_assets_matched")


class HostedRecordedService(StudioService):
    async def checked_run(self, run_id: UUID) -> StudioRun:
        recorded_bundle(self.config)
        run = await self.store.get_run(run_id)
        async with self.store.factory() as session:
            planner = await session.get(StudioPlannerRun, run_id)
        if (
            run.mode != "recorded"
            or planner is None
            or planner.configuration.get("recorded_final_reuse") is not True
            or planner.configuration.get("bundle_sha256") != self.config.recorded_bundle_sha256
        ):
            raise StudioError("recorded_bundle_changed", 409)
        return run

    async def ideate(self, run_id: UUID) -> None:
        from myloware.studio.recorded import RecordedMediaArchive

        run = await self.checked_run(run_id)
        if run.status != "ideating":
            return
        plan = RecordedMediaArchive(self.config.recorded_root).plan_for(
            run_id=run_id, revision=run.revision, item_text=run.item_text
        )
        await self.store.save_plan(plan, {"source": "recorded_archive", "fresh_check": False})
        await self.observed(run_id, "recorded_plan_loaded")

    def assets(self, run_id: UUID, *, run: StudioRun | None = None) -> HostedRecordedAssets:
        return HostedRecordedAssets(self)

    async def require_assets(self, run_id: UUID) -> None:
        from myloware.studio.recorded import RecordedMediaArchive

        run = await self.checked_run(run_id)
        recorded_plan = RecordedMediaArchive(self.config.recorded_root).plan_for(
            run_id=run_id, revision=run.revision, item_text=run.item_text
        )
        if (
            run.plan_hash != recorded_plan.canonical_sha256
            or run.plan_approved_hash != run.plan_hash
        ):
            raise StudioError("recorded_plan_mismatch", 409)
        async with self.store.factory() as session:
            assets = (
                await session.scalars(
                    select(StudioAsset).where(
                        StudioAsset.run_id == run_id, StudioAsset.revision == run.revision
                    )
                )
            ).all()
        if len(assets) != 24 or len({(a.ordinal, a.kind) for a in assets}) != 24:
            raise StudioError("recorded_assets_mismatch", 409)
        for asset in assets:
            role = {
                "video": "recorded_video_provider_output",
                "voice": "recorded_voice_provider_output_pause_split",
            }.get(str(asset.kind))
            if (
                role is None
                or asset.status != "ready"
                or asset.provider != "recorded_archive"
                or asset.sha256 != bundle_entry(self.config, role, asset.ordinal)["sha256"]
                or asset.input_hash != run.plan_approved_hash
            ):
                raise StudioError("recorded_assets_mismatch", 409)

    async def submit_edit(self, run_id: UUID) -> None:
        run = await self.checked_run(run_id)
        if run.status != "editing" or run.render_submission_state:
            return
        await self.require_assets(run_id)
        final = bundle_entry(self.config, "approved_local_render")
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            if current.status != "editing" or current.render_submission_state:
                return
            current.render_input_hash = digest(
                {
                    "plan": current.plan_approved_hash,
                    "bundle": self.config.recorded_bundle_sha256,
                    "final": final["sha256"],
                }
            )
            current.render_job_id = f"recorded:{final['sha256']}"
            current.render_submission_state = "reused"
            await self.store.event(session, current, "editing", "recorded_final_selected")

    async def inspect_edit(self, run_id: UUID) -> None:
        run = await self.checked_run(run_id)
        if run.status != "editing" or run.render_submission_state != "reused":
            return
        await self.require_assets(run_id)
        final = bundle_entry(self.config, "approved_local_render")
        expected = digest(
            {
                "plan": run.plan_approved_hash,
                "bundle": self.config.recorded_bundle_sha256,
                "final": final["sha256"],
            }
        )
        if run.render_input_hash != expected or run.render_job_id != f"recorded:{final['sha256']}":
            raise StudioError("recorded_final_mismatch", 409)
        from myloware.storage.object_store import get_s3_store

        verified = await get_s3_store().verify_object_async(
            uri=object_uri(self.config, final),
            expected_sha256=final["sha256"],
            expected_bytes=final["byte_size"],
        )
        stream = next(value for value in final["streams"] if value["codec_type"] == "video")
        metadata = {
            "media_verified": True,
            "verification": "r2_stream_sha256",
            "sha256": final["sha256"],
            "byte_size": final["byte_size"],
            "duration_seconds": final["duration_seconds"],
            "width": stream["width"],
            "height": stream["height"],
            "recorded": True,
            "recorded_final_reuse": True,
            "rendered_now": False,
            "moderated_now": False,
            "bundle_sha256": self.config.recorded_bundle_sha256,
            "object_etag": verified["etag"],
        }
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            if current.status != "editing" or current.render_input_hash != expected:
                return
            artifact = Artifact(
                run_id=run_id,
                persona="recorded_archive",
                artifact_type="recorded_final_video",
                uri=object_uri(self.config, final),
                artifact_metadata=metadata,
            )
            session.add(artifact)
            await session.flush()
            current.final_artifact_id = artifact.id
            current.final_hash = final["sha256"]
            current.final_metadata = metadata
            current.status = "final_review"
            current.final_review_created_at = _utc_now()
            current.final_review_expires_at = _utc_now() + timedelta(
                hours=self.config.final_review_hours
            )
            await self.store.event(session, current, "final_review", "recorded_final_loaded")
