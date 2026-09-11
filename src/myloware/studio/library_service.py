"""SQL-owned gallery selection and serialized cleanup of local run media."""

from __future__ import annotations

import asyncio
from datetime import UTC
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from myloware.config import settings
from myloware.storage.studio_models import (
    StudioCostLedger,
    StudioDecision,
    StudioGalleryProjectionIntent,
    StudioReservation,
    StudioRun,
)
from myloware.storage.studio_store import StudioError, StudioStore
from myloware.studio.library import LibraryCandidate, RetentionPlan, StudioLibrary
from myloware.workers.claims import require_current_claim


class StudioLibraryService:
    def __init__(self, store: StudioStore) -> None:
        self.store = store
        config = store.config
        if config.recorded_root is not None:
            archive = config.recorded_root.resolve()
            for configured in (config.media_root, config.fixture_root):
                root = configured.resolve()
                if root == archive or root in archive.parents or archive in root.parents:
                    raise StudioError("retention_archive_overlap")
        self.files = StudioLibrary(media_root=config.media_root, fixture_root=config.fixture_root)

    async def _candidates(
        self, session: AsyncSession
    ) -> tuple[list[StudioRun], list[LibraryCandidate]]:
        runs = (await session.scalars(select(StudioRun))).all()
        approvals = dict(
            (
                await session.execute(
                    select(StudioDecision.run_id, func.max(StudioDecision.created_at))
                    .where(StudioDecision.gate == "final", StudioDecision.decision == "approve")
                    .group_by(StudioDecision.run_id)
                )
            ).all()
        )
        projected = set(await session.scalars(select(StudioGalleryProjectionIntent.run_id)))
        uncertain = set(
            await session.scalars(
                select(StudioCostLedger.run_id).where(
                    StudioCostLedger.cost_state.in_(("unknown", "submission_unknown"))
                )
            )
        )
        uncertain.update(
            await session.scalars(
                select(StudioReservation.run_id).where(
                    StudioReservation.cost_state.in_(("unknown", "submission_unknown"))
                )
            )
        )
        local_recorded = self.store.config.mode == "recorded" and urlsplit(
            self.store.config.origin
        ).hostname in {"localhost", "127.0.0.1", "::1"}
        candidates = [
            LibraryCandidate(
                run_id=run.run_id,
                status=run.status,
                mode=run.mode,
                final_hash=run.final_hash,
                final_metadata=run.final_metadata,
                completed_at=(
                    approvals.get(run.run_id)
                    if run.mode == "fixture" or (run.mode == "recorded" and local_recorded)
                    else None
                ),
            )
            for run in runs
            if run.run_id not in projected and run.run_id not in uncertain
        ]
        return list(runs), candidates

    async def gallery(self) -> list[dict[str, Any]]:
        async with self.store.factory() as session:
            runs, candidates = await self._candidates(session)
        retained = await asyncio.to_thread(self.files.retained, candidates)
        by_id = {str(run.run_id): run for run in runs}
        return [
            {
                "id": str(candidate.run_id),
                "item": by_id[str(candidate.run_id)].item_text,
                "mode": candidate.mode,
                "sha256": candidate.final_hash,
                "created_at": (
                    candidate.completed_at.replace(tzinfo=UTC).isoformat()
                    if candidate.completed_at
                    else None
                ),
                "duration_seconds": (candidate.final_metadata or {}).get("duration_seconds"),
                "url": f"/v1/studio/gallery/{candidate.run_id}/video",
            }
            for candidate in retained
        ]

    async def cleanup(self, trigger_run_id: UUID) -> None:
        # Hosted samples are shared immutable R2 objects, never per-run files.
        if self.store.config.recorded_final_reuse:
            return
        # Private live evidence retention is an explicit, configuration-validated
        # exception for an owner-run trial. Keep all operational source files and
        # the renderer output; gallery selection remains independently capped.
        if self.store.config.preserve_run_artifacts:
            return
        # Hashing large finals does not hold the global admission write lock.
        # The exact candidate snapshot is compared again inside that lock before
        # its plan becomes durable, so concurrent approval cannot select a stale
        # eviction set.
        while True:
            async with self.store.factory() as session:
                _, candidates = await self._candidates(session)
            plan = await asyncio.to_thread(self.files.plan, candidates)
            async with self.store.transaction() as session:
                runs, current = await self._candidates(session)
                if not self._same_candidates(candidates, current):
                    continue
                # The transaction durably records the plan. The filesystem
                # mutation happens only after that commit, so a crash can resume
                # its exact bounded action without reclassifying missing files.
                self._record_intents(runs, plan)
                break

        # A cleanup worker can lose its lease while its intent transaction was
        # committing. Check the native claim again immediately before bytes move.
        async with self.store.factory() as session, session.begin():
            await require_current_claim(session)
        receipt = await asyncio.to_thread(self.files.apply, plan)

        async with self.store.transaction() as session:
            runs, _ = await self._candidates(session)
            self._complete_intents(runs, plan)
            trigger = next((run for run in runs if run.run_id == trigger_run_id), None)
            if trigger is not None and receipt.removed_paths:
                await self.store.event(
                    session,
                    trigger,
                    trigger.status,
                    "library_pruned",
                    {
                        "kept": len(receipt.kept_run_ids),
                        "evicted": len(receipt.evicted_run_ids),
                        "removed_entries": len(receipt.removed_paths),
                        "removed_bytes": receipt.removed_bytes,
                    },
                )
        if (
            trigger is not None
            and trigger.status == "video_complete"
            and trigger.run_id in receipt.kept_run_ids
        ):
            await self._renderer_copy(trigger)

    @staticmethod
    def _same_candidates(prepared: list[LibraryCandidate], current: list[LibraryCandidate]) -> bool:
        return {candidate.run_id: candidate for candidate in prepared} == {
            candidate.run_id: candidate for candidate in current
        }

    @staticmethod
    def _record_intents(runs: list[StudioRun], plan: RetentionPlan) -> None:
        actions = {
            **{candidate.run_id: "keep" for candidate in plan.kept},
            **{candidate.run_id: "evict" for candidate in plan.evicted},
            **{candidate.run_id: "remove" for candidate in plan.terminal},
        }
        for run in runs:
            action = actions.get(UUID(str(run.run_id)))
            if action is None:
                continue
            metadata = dict(run.final_metadata or {})
            existing = metadata.get("retention_cleanup")
            if isinstance(existing, dict) and existing.get("state") == "pending":
                if existing.get("action") == "keep" and action == "evict":
                    metadata["retention_cleanup"] = {"state": "pending", "action": "evict"}
                    run.final_metadata = metadata
                continue
            metadata["retention_cleanup"] = {"state": "pending", "action": action}
            run.final_metadata = metadata

    @staticmethod
    def _complete_intents(runs: list[StudioRun], plan: RetentionPlan) -> None:
        actions = {
            **{candidate.run_id: "keep" for candidate in plan.kept},
            **{candidate.run_id: "evict" for candidate in plan.evicted},
            **{candidate.run_id: "remove" for candidate in plan.terminal},
            **{candidate.run_id: "keep" for candidate in plan.noop},
        }
        for run in runs:
            action = actions.get(UUID(str(run.run_id)))
            metadata = dict(run.final_metadata or {})
            intent = metadata.get("retention_cleanup")
            if action is None or not isinstance(intent, dict) or intent.get("state") != "pending":
                continue
            if intent.get("action") != action:
                continue
            if action == "evict":
                metadata["retention"] = "evicted"
            metadata.pop("retention_cleanup", None)
            run.final_metadata = metadata

    async def _renderer_copy(self, run: StudioRun) -> None:
        if not run.render_job_id or not run.render_input_hash or not run.final_hash:
            return
        metadata: dict[str, Any] = dict(run.final_metadata or {})
        if metadata.get("renderer_copy_removed") is True:
            return
        secret = settings.remotion_api_secret
        if not secret:
            return
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.request(
                "DELETE",
                f"{settings.remotion_service_url.rstrip('/')}/api/render/{quote(str(run.render_job_id), safe='')}/output",
                headers={"Authorization": f"Bearer {secret}", "x-api-key": secret},
                json={
                    "run_id": str(run.run_id),
                    "input_hash": run.render_input_hash,
                    "expected_sha256": run.final_hash,
                },
            )
        if response.status_code == 404:
            # A restarted renderer cannot prove ownership of its old jobs. Its
            # output TTL remains the fallback; never guess an output path here.
            async with self.store.transaction() as session:
                current = await session.get(StudioRun, run.run_id)
                current_metadata: dict[str, Any] = (
                    dict(current.final_metadata or {}) if current is not None else {}
                )
                if current is not None and not current_metadata.get("renderer_cleanup_deferred"):
                    current.final_metadata = {
                        **(current.final_metadata or {}),
                        "renderer_cleanup_deferred": True,
                    }
                    await self.store.event(
                        session, current, current.status, "renderer_cleanup_deferred"
                    )
            return
        if response.status_code != 200 or response.json().get("status") not in {
            "deleted",
            "already_deleted",
        }:
            raise StudioError("renderer_cleanup_failed")
        async with self.store.transaction() as session:
            current = await session.get(StudioRun, run.run_id)
            current_metadata = dict(current.final_metadata or {}) if current is not None else {}
            if current is not None and not current_metadata.get("renderer_copy_removed"):
                current.final_metadata = {
                    **(current.final_metadata or {}),
                    "renderer_copy_removed": True,
                }
                await self.store.event(session, current, current.status, "renderer_copy_removed")
