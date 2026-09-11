"""Compose durable creative planning inside the existing idea-review boundary."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from myloware.storage.studio_models import StudioPlannerRun
from myloware.storage.studio_store import StudioError
from myloware.studio.creative_planning import CreativePlanningError
from myloware.studio.creative_repair import CreativeRepairError
from myloware.studio.fixtures import prepare_fixture_media
from myloware.studio.moderation import ModerationVerdict
from myloware.studio.planning_store import (
    DurablePlanning,
    pinned_repair_attempts,
    pinned_workflow_deadline,
)
from myloware.workflows.scenes import ScenePlan, deterministic_scene_plan

if TYPE_CHECKING:
    from myloware.storage.studio_models import StudioRun
    from myloware.studio.service import StudioService


async def ideate_creative(service: StudioService, run: StudioRun) -> None:
    operations = DurablePlanning(service.store, run.run_id, run.revision)
    try:
        context = await operations.context()
        async with service.store.factory() as session:
            profile = await session.get(StudioPlannerRun, run.run_id)
            if profile is None:
                raise StudioError("planner_profile_missing")
            pinned = dict(profile.configuration)
        if run.mode in {"live", "planning", "local"} and (
            pinned["backend"] != service.config.ideation_backend
            or service.creative_planner is None
            or pinned["model"] != service.creative_planner._model
        ):
            raise StudioError("planner_profile_mismatch")

        async def moderate(text: str, stage: str) -> ModerationVerdict:
            async def produce() -> dict[str, Any]:
                return asdict(await service.moderator.moderate_text(text, stage=stage))

            raw = await operations(
                operation_name=f"{stage}_moderation",
                input_payload={"text": text},
                response_schema={},
                model="content-safety",
                prompt={"text": text},
                limits={
                    "max_input_bytes": 32_768,
                    "max_output_bytes": 4096,
                    "max_output_tokens": 0,
                    "deadline_seconds": pinned["deadline_seconds"],
                },
                metadata={"stage": stage},
                producer=produce,
            )
            if not isinstance(raw, dict):
                raise StudioError("moderation_receipt_invalid")
            return ModerationVerdict(**raw)

        input_text = run.item_text
        if context.revision_feedback:
            input_text += "\nRevision feedback: " + "; ".join(
                f"{entry.category}: {entry.note or ''}" for entry in context.revision_feedback
            )
        verdict = await moderate(input_text, "input")
        if not verdict.safe:
            await service.store.stop(run.run_id, "input_moderation_blocked")
            return
        if run.mode == "fixture":
            # These remain explicitly sample plans and do not imply an AI call.
            plan = deterministic_scene_plan(run.run_id, run.item_text, run.revision)
            retained = {entry.ordinal: entry.idea for entry in context.retained_ideas}
            if run.revision > 1:
                scenes = tuple(
                    retained.get(idea.ordinal)
                    or idea.model_copy(
                        update={
                            "title": f"Sample{run.revision}{idea.ordinal} {idea.title.split(' ', 1)[1]}",
                            "visual_prompt": f"Sample revision {run.revision}. {idea.visual_prompt}",
                        }
                    )
                    for idea in plan.scenes
                )
                plan = ScenePlan.model_validate(plan.model_dump() | {"scenes": scenes})
        else:
            if service.creative_planner is None:
                raise StudioError("creative_planner_unavailable")
            plan = await service.creative_planner.create_plan(
                run_id=run.run_id,
                revision=run.revision,
                item=run.item_text,
                context=context,
                run_operation=operations,
                deadline_seconds=pinned["deadline_seconds"],
                workflow_deadline_seconds=pinned_workflow_deadline(pinned),
                repair_attempts=pinned_repair_attempts(pinned),
            )
        output = await moderate(plan.model_dump_json(), "plan")
        if not output.safe:
            await service.store.stop(run.run_id, "plan_moderation_blocked")
            return
        if run.mode == "fixture":
            await prepare_fixture_media(plan, service.config.fixture_root)
        await service.store.save_plan(
            plan,
            {
                "input": asdict(verdict),
                "plan": asdict(output),
                "plan_hash": plan.canonical_sha256,
                "planner_version": "creative-v2",
                "context_sha256": context.sha256,
            },
        )
    except (StudioError, CreativePlanningError, ValueError) as exc:
        current = await service.store.get_run(run.run_id)
        if current.status == "ideating":
            code = (
                exc.code
                if isinstance(exc, (StudioError, CreativeRepairError))
                else "creative_plan_invalid"
            )
            await service.store.stop(run.run_id, code)
        raise
