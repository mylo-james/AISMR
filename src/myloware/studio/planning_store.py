"""SQL-owned creative inputs, operation receipts and bounded revision feedback."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import select, update

from myloware.storage.models import _utc_now
from myloware.storage.studio_models import (
    StudioCostLedger,
    StudioDecision,
    StudioEvent,
    StudioPlannerRun,
    StudioPlanningOperation,
    StudioRun,
)
from myloware.storage.studio_store import StudioError, StudioStore, digest
from myloware.studio.creative_planning import (
    PLANNER_VERSION,
    PLANNING_ROLE_LIMITS,
    PLANNING_ROLES,
    CreativePlanner,
    CreativePlanningContext,
    RecentConcept,
    RetainedIdea,
    RevisionFeedback,
    _Selection,
)
from myloware.studio.creative_repair import (
    MAX_REPAIR_ATTEMPTS,
    REPAIR_OPERATION_NAMES,
    REPAIR_PROMPT_VERSION,
    REPAIR_SCHEMA_VERSION,
    source_response_sha256,
)
from myloware.studio.execution_profile import require_plan_execution
from myloware.workflows.scenes import parse_plan


def planning_contract(*, repair_attempts: int = 0) -> dict[str, Any]:
    """Fingerprint the resource and prompt contract that admission authorizes."""
    if type(repair_attempts) is not int or not 0 <= repair_attempts <= MAX_REPAIR_ATTEMPTS:
        raise StudioError("planner_repair_policy_invalid")
    contract: dict[str, Any] = {
        "version": PLANNER_VERSION,
        "prompt_version": CreativePlanner.prompt_version,
        "schema_version": CreativePlanner.schema_version,
        "role_limits": PLANNING_ROLE_LIMITS,
    }
    if repair_attempts:
        contract["repair_policy"] = {
            "version": "targeted-validation-repair-v1",
            "attempts": repair_attempts,
            "roles": REPAIR_OPERATION_NAMES[:repair_attempts],
            "prompt_version": REPAIR_PROMPT_VERSION,
            "schema_version": REPAIR_SCHEMA_VERSION,
            "limits": "source_role",
            "order": "object_then_surreal_then_sequential",
        }
    return contract


def pinned_repair_attempts(configuration: Mapping[str, Any]) -> int:
    """Old profiles have no repair allowance; new profiles bind it in their contract."""
    attempts = configuration.get("repair_attempts", 0)
    if type(attempts) is not int or not 0 <= attempts <= MAX_REPAIR_ATTEMPTS:
        raise StudioError("planner_repair_policy_invalid")
    return attempts


def compatible_planning_contract(configuration: Mapping[str, Any]) -> bool:
    try:
        return configuration.get("contract_sha256") == digest(
            planning_contract(repair_attempts=pinned_repair_attempts(configuration))
        )
    except StudioError:
        return False


def planning_deadline_policy(
    *, deadline_seconds: int, creative_workflow_deadline_seconds: int
) -> dict[str, int | str]:
    """Fingerprint the separately pinned per-call and whole-workflow time limits."""
    return {
        "version": "creative-workflow-deadline-v1",
        "per_call_deadline_seconds": deadline_seconds,
        "workflow_deadline_seconds": creative_workflow_deadline_seconds,
        "critical_path_provider_stages": 5,
        "durable_moderation_allowance_seconds": 60,
    }


def pinned_workflow_deadline(configuration: Mapping[str, Any]) -> int:
    """Read the immutable workflow deadline, preserving pre-policy receipts."""
    per_call = configuration.get("deadline_seconds")
    workflow = configuration.get("creative_workflow_deadline_seconds", per_call)
    if (
        isinstance(per_call, bool)
        or not isinstance(per_call, int)
        or not 1 <= per_call <= 120
        or isinstance(workflow, bool)
        or not isinstance(workflow, int)
        or not 1 <= workflow <= 660
    ):
        raise StudioError("planner_deadline_policy_invalid")
    fingerprint = configuration.get("deadline_policy_sha256")
    if fingerprint is not None and fingerprint != digest(
        planning_deadline_policy(
            deadline_seconds=per_call,
            creative_workflow_deadline_seconds=workflow,
        )
    ):
        raise StudioError("planner_deadline_policy_mismatch")
    return workflow


async def purge_expired_planning_data(
    session: Any, *, now: datetime, preserve_artifacts: bool = False
) -> None:
    """Expire creative content while retaining operation identity and cost evidence."""
    if preserve_artifacts:
        return
    await session.execute(
        update(StudioPlanningOperation)
        .where(
            (StudioPlanningOperation.expires_at <= now)
            | StudioPlanningOperation.run_id.in_(
                select(StudioPlannerRun.run_id).where(StudioPlannerRun.expires_at <= now)
            ),
        )
        .values(request={}, response=None)
    )
    decisions = (
        await session.scalars(
            select(StudioDecision)
            .join(
                StudioPlannerRun,
                StudioPlannerRun.run_id == StudioDecision.run_id,
            )
            .where(StudioPlannerRun.expires_at <= now)
        )
    ).all()
    for decision in decisions:
        payload = dict(decision.payload or {})
        payload.pop("previous_plan", None)
        payload.pop("revision_feedback", None)
        decision.payload = payload


class PlanRevisionRequest(BaseModel):
    """Visitor feedback is data bound to an authenticated exact-plan decision."""

    model_config = ConfigDict(extra="forbid")
    replace_ordinals: list[StrictInt] = Field(
        default_factory=lambda: list(range(1, 13)), min_length=1, max_length=12
    )
    reason: Literal["repetitive", "not_object_specific", "unclear_action", "other"] = "other"
    note: str = Field(default="", max_length=500)

    @field_validator("replace_ordinals")
    @classmethod
    def validate_ordinals(cls, value: list[int]) -> list[int]:
        if any(ordinal not in range(1, 13) for ordinal in value) or len(set(value)) != len(value):
            raise ValueError("replacement ordinals must be unique scene positions")
        return sorted(value)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str) -> str:
        return value.strip()


async def planner_version(store: StudioStore, run_id: UUID) -> str:
    async with store.factory() as session:
        record = await session.get(StudioPlannerRun, run_id)
        return str(record.version) if record is not None else "single-v1"


def _value(response: Any) -> Any:
    value = response.get("value") if isinstance(response, dict) else None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _validated_v3_ranked_selection(
    *,
    output: dict[str, Any],
    request: dict[str, Any],
    input_payload: dict[str, Any],
    candidate_ids: set[str],
    role: str,
) -> list[tuple[str, int]] | None:
    """Return v3-v5 rank assignments only when the complete saved receipt is usable."""
    metadata = request.get("metadata")
    replacement_ordinals = input_payload.get("replacement_ordinals")
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema_version") not in (3, 4, 5)
        or not isinstance(replacement_ordinals, list)
        or not replacement_ordinals
        or any(
            not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal not in range(1, 13)
            for ordinal in replacement_ordinals
        )
        or len(set(replacement_ordinals)) != len(replacement_ordinals)
    ):
        return None
    try:
        ranked = _Selection.model_validate(output).ranked
    except ValueError:
        return None
    if len(ranked) > len(replacement_ordinals) or (
        role == "recurate" and len(ranked) != len(replacement_ordinals)
    ):
        return None
    ids = [choice.candidate_id for choice in ranked]
    if len(set(ids)) != len(ids) or any(candidate_id not in candidate_ids for candidate_id in ids):
        return None
    return list(zip(ids, replacement_ordinals, strict=False))


class DurablePlanning:
    """Replay known results; a durable intent without a result remains uncertain."""

    def __init__(self, store: StudioStore, run_id: UUID, revision: int) -> None:
        self.store, self.run_id, self.revision = store, run_id, revision

    async def context(self) -> CreativePlanningContext:
        async with self.store.transaction() as session:
            run, profile = await self._current(session)
            saved = await self._operation(session, "prepare_context")
            if saved is not None:
                return CreativePlanningContext.model_validate(saved.response["context"])
            retained: tuple[RetainedIdea, ...] = ()
            retained_hash = None
            feedback: tuple[RevisionFeedback, ...] = ()
            if self.revision > 1:
                decision = await session.scalar(
                    select(StudioDecision).where(
                        StudioDecision.run_id == self.run_id,
                        StudioDecision.visitor_id == run.visitor_id,
                        StudioDecision.revision == self.revision - 1,
                        StudioDecision.gate == "ideas",
                        StudioDecision.decision == "revise",
                    )
                )
                if decision is None or decision.expires_at <= _utc_now():
                    raise StudioError("revision_context_missing")
                payload = dict(decision.payload or {})
                previous = parse_plan(payload.get("previous_plan"))
                require_plan_execution(previous, run.execution_profile)
                if previous.canonical_sha256 != decision.subject_hash:
                    raise StudioError("revision_context_mismatch")
                request = PlanRevisionRequest.model_validate(payload.get("revision_feedback", {}))
                retained = tuple(
                    RetainedIdea(ordinal=idea.ordinal, idea=idea)
                    for idea in previous.ideas
                    if idea.ordinal not in request.replace_ordinals
                )
                retained_hash = previous.canonical_sha256 if retained else None
                categories = {
                    "repetitive": "more_variety",
                    "not_object_specific": "more_physical",
                    "unclear_action": "clearer_action",
                    "other": "more_original",
                }
                feedback = (
                    RevisionFeedback(
                        category=categories[request.reason], note=request.note or None
                    ),
                )
            recent = await self._recent(session, run)
            context = CreativePlanningContext(
                recent_concepts=tuple(recent),
                retained_ideas=retained,
                retained_plan_hash=retained_hash,
                revision_feedback=feedback,
            )
            now = _utc_now()
            session.add(
                StudioPlanningOperation(
                    run_id=self.run_id,
                    revision=self.revision,
                    role="prepare_context",
                    input_hash=context.sha256,
                    request={"version": PLANNER_VERSION},
                    response={"context": context.model_dump(mode="json")},
                    status="ready",
                    deadline_at=now
                    + timedelta(seconds=pinned_workflow_deadline(profile.configuration)),
                    completed_at=now,
                    expires_at=profile.expires_at,
                )
            )
            await self.store.event(
                session,
                run,
                "ideating",
                "creative_context_prepared",
                {
                    "revision": self.revision,
                    "count": len(recent),
                    "kept": len(retained),
                },
            )
            return context

    async def _recent(self, session: Any, run: StudioRun) -> list[RecentConcept]:
        """Read only this visitor's unexpired selected cards, never global history."""
        records = (
            await session.scalars(
                select(StudioPlanningOperation)
                .join(StudioRun, StudioRun.run_id == StudioPlanningOperation.run_id)
                .where(
                    StudioRun.visitor_id == run.visitor_id,
                    StudioRun.item_id == run.item_id,
                    StudioPlanningOperation.expires_at > _utc_now(),
                    StudioPlanningOperation.status == "ready",
                    StudioPlanningOperation.role.in_({"curate", "recurate"}),
                    # A new revision may learn from its own immediately previous set.
                    (StudioRun.created_at < run.created_at)
                    | (
                        (StudioRun.run_id == run.run_id)
                        & (StudioPlanningOperation.revision < self.revision)
                    ),
                )
                .order_by(StudioPlanningOperation.created_at.desc())
                .limit(6)
            )
        ).all()
        recent: list[RecentConcept] = []
        seen: set[tuple[str, str, str]] = set()
        for record in records:
            output = _value(record.response)
            if not isinstance(output, dict):
                output = {}
            request = record.request if isinstance(record.request, dict) else {}
            input_payload = request.get("input_payload")
            selected_with_ordinals: list[tuple[object, object]] = []
            if isinstance(output.get("selected"), list):
                # Schema v1/v2 receipts persist model-assigned calendar positions.
                selected_with_ordinals = [
                    (entry.get("candidate_id"), entry.get("ordinal"))
                    for entry in output["selected"]
                    if isinstance(entry, dict)
                ]
            cards = input_payload.get("cards", []) if isinstance(input_payload, dict) else []
            by_id: dict[str, dict[str, Any]] = {}
            if isinstance(cards, list):
                for card in cards:
                    candidate_id = card.get("candidate_id") if isinstance(card, dict) else None
                    if isinstance(candidate_id, str) and candidate_id:
                        by_id[candidate_id] = card
            if not selected_with_ordinals:
                if not isinstance(input_payload, dict):
                    continue
                selected_v3 = _validated_v3_ranked_selection(
                    output=output,
                    request=request,
                    input_payload=input_payload,
                    candidate_ids=set(by_id),
                    role=str(record.role),
                )
                if selected_v3 is None:
                    selected_v3 = await self._repaired_selection(
                        session, record, request, input_payload, by_id
                    )
                if selected_v3 is None:
                    continue
                selected_with_ordinals = selected_v3
            decisions = (
                await session.scalars(
                    select(StudioDecision).where(
                        StudioDecision.run_id == record.run_id,
                        StudioDecision.visitor_id == run.visitor_id,
                        StudioDecision.revision == record.revision,
                        StudioDecision.gate == "ideas",
                    )
                )
            ).all()
            outcome = "approved" if any(d.decision == "approve" for d in decisions) else "unknown"
            revised_ordinals: set[int] = set()
            for decision in decisions:
                if decision.decision == "revise":
                    try:
                        revised_ordinals.update(
                            PlanRevisionRequest.model_validate(
                                (decision.payload or {}).get("revision_feedback", {})
                            ).replace_ordinals
                        )
                    except (TypeError, ValueError):
                        continue
            for candidate_id, ordinal in selected_with_ordinals:
                if (
                    not isinstance(candidate_id, str)
                    or not candidate_id
                    or not isinstance(ordinal, int)
                    or isinstance(ordinal, bool)
                ):
                    continue
                card = by_id.get(candidate_id)
                if card is None:
                    continue
                material, action, setting = (
                    card.get("material"),
                    card.get("action"),
                    card.get("setting"),
                )
                if not all(
                    isinstance(value, str) and value for value in (material, action, setting)
                ):
                    continue
                key = (material, action, setting)
                if key in seen:
                    continue
                try:
                    concept = RecentConcept(
                        material=key[0],
                        action=key[1],
                        setting=key[2],
                        outcome="revised" if ordinal in revised_ordinals else outcome,
                    )
                except ValueError:
                    continue
                seen.add(key)
                recent.append(concept)
                if len(recent) == 12:
                    return recent
        return recent

    async def _repaired_selection(
        self,
        session: Any,
        record: StudioPlanningOperation,
        request: dict[str, Any],
        input_payload: dict[str, Any],
        cards: dict[str, dict[str, Any]],
    ) -> list[tuple[str, int]] | None:
        """Use the backend's validated writer input after a repaired curation.

        Original invalid receipts stay immutable. The writer request is created
        only after selection validation, so it supplies the accepted rank order.
        Require a matching correction receipt and exact offered card content.
        """
        repairs = (
            await session.scalars(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == record.run_id,
                    StudioPlanningOperation.revision == record.revision,
                    StudioPlanningOperation.role.in_(REPAIR_OPERATION_NAMES),
                    StudioPlanningOperation.status == "ready",
                )
            )
        ).all()
        if "value" not in (record.response or {}):
            return None
        source_hash = source_response_sha256(record.response["value"])
        if not any(
            isinstance(repair.request, dict)
            and digest(repair.request) == repair.input_hash
            and isinstance(repair.request.get("input_payload"), dict)
            and repair.request["input_payload"].get("source_role") == record.role
            and repair.request["input_payload"].get("source_response_sha256") == source_hash
            for repair in repairs
        ):
            return None
        writer = await session.scalar(
            select(StudioPlanningOperation).where(
                StudioPlanningOperation.run_id == record.run_id,
                StudioPlanningOperation.revision == record.revision,
                StudioPlanningOperation.role == "write_shots",
                StudioPlanningOperation.status == "ready",
            )
        )
        if (
            writer is None
            or not isinstance(writer.request, dict)
            or digest(writer.request) != writer.input_hash
            or writer.expires_at <= _utc_now()
        ):
            return None
        payload = writer.request.get("input_payload")
        if not isinstance(payload, dict):
            return None
        selected, offered = payload.get("selected"), payload.get("cards")
        if not isinstance(selected, list) or not isinstance(offered, list):
            return None
        if any(
            not isinstance(card, dict)
            or not isinstance(card.get("candidate_id"), str)
            or cards.get(card["candidate_id"]) != card
            for card in offered
        ):
            return None
        if any(
            not isinstance(entry, dict) or type(entry.get("ordinal")) is not int
            for entry in selected
        ):
            return None
        if [entry.get("ordinal") for entry in selected] != input_payload.get(
            "replacement_ordinals"
        ):
            return None
        return _validated_v3_ranked_selection(
            output={
                "ranked": [
                    {"candidate_id": entry.get("candidate_id"), "reason": entry.get("reason")}
                    for entry in selected
                ]
            },
            request=request,
            input_payload=input_payload,
            candidate_ids=set(cards),
            role=str(record.role),
        )

    async def _current(self, session: Any) -> tuple[StudioRun, StudioPlannerRun]:
        run = await session.get(StudioRun, self.run_id)
        profile = await session.get(StudioPlannerRun, self.run_id)
        if (
            run is None
            or run.cancelled
            or run.status != "ideating"
            or run.revision != self.revision
            or run.expires_at <= _utc_now()
        ):
            raise StudioError("planning_no_longer_current")
        if (
            profile is None
            or profile.version != PLANNER_VERSION
            or profile.expires_at <= _utc_now()
        ):
            raise StudioError("planner_profile_unavailable")
        if not compatible_planning_contract(profile.configuration):
            raise StudioError("planner_contract_mismatch")
        pinned_workflow_deadline(profile.configuration)
        return run, profile

    async def _operation(self, session: Any, role: str) -> StudioPlanningOperation | None:
        return await session.scalar(
            select(StudioPlanningOperation).where(
                StudioPlanningOperation.run_id == self.run_id,
                StudioPlanningOperation.revision == self.revision,
                StudioPlanningOperation.role == role,
            )
        )

    async def __call__(
        self,
        *,
        operation_name: str,
        input_payload: Mapping[str, Any],
        response_schema: Mapping[str, Any],
        model: str,
        prompt: Mapping[str, str],
        limits: Mapping[str, int],
        metadata: Mapping[str, Any],
        producer: Callable[[], Awaitable[Mapping[str, Any] | str]],
    ) -> Mapping[str, Any] | str:
        is_repair = operation_name in REPAIR_OPERATION_NAMES
        creative_role = operation_name in PLANNING_ROLES or is_repair
        if operation_name not in {
            *PLANNING_ROLES,
            *REPAIR_OPERATION_NAMES,
            "input_moderation",
            "plan_moderation",
        }:
            raise StudioError("unknown_planning_role")
        source_role = input_payload.get("source_role") if is_repair else operation_name
        if not isinstance(source_role, str) or (is_repair and source_role not in PLANNING_ROLES):
            raise StudioError("planning_repair_source_invalid")
        if creative_role and any(
            limits.get(key) != value for key, value in PLANNING_ROLE_LIMITS[source_role].items()
        ):
            raise StudioError("planner_limits_mismatch")
        request = {
            "input_payload": dict(input_payload),
            "response_schema": dict(response_schema),
            "model": model,
            "prompt": dict(prompt),
            "limits": dict(limits),
            "metadata": dict(metadata),
        }
        input_hash = digest(request)
        size = len(json.dumps(dict(prompt), ensure_ascii=False).encode())
        if size > limits["max_input_bytes"] + 100 or len(json.dumps(request).encode()) > 160_000:
            raise StudioError("planning_input_limit")
        unknown = False
        async with self.store.transaction() as session:
            run, profile = await self._current(session)
            if creative_role and (
                model != profile.configuration["model"]
                or metadata.get("profile_version") != profile.version
                or metadata.get("run_id") != str(self.run_id)
                or metadata.get("revision") != self.revision
                or metadata.get("prompt_version")
                != (REPAIR_PROMPT_VERSION if is_repair else CreativePlanner.prompt_version)
                or metadata.get("schema_version")
                != (REPAIR_SCHEMA_VERSION if is_repair else CreativePlanner.schema_version)
                or metadata.get("operation_key")
                != f"{self.run_id}:{self.revision}:{operation_name}"
                or metadata.get("input_sha256")
                != sha256(
                    json.dumps(input_payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            ):
                raise StudioError("planner_profile_mismatch")
            repair_detail: dict[str, Any] = {}
            if is_repair:
                attempt = REPAIR_OPERATION_NAMES.index(operation_name) + 1
                if attempt > pinned_repair_attempts(profile.configuration):
                    raise StudioError("planning_repair_not_authorized")
                if (
                    type(input_payload.get("attempt")) is not int
                    or input_payload.get("attempt") != attempt
                    or type(metadata.get("attempt")) is not int
                    or metadata.get("attempt") != attempt
                    or metadata.get("source_role") != source_role
                ):
                    raise StudioError("planning_repair_identity_mismatch")
                source = await self._operation(session, source_role)
                if (
                    source is None
                    or source.status != "ready"
                    or "value" not in (source.response or {})
                ):
                    raise StudioError("planning_repair_source_unavailable")
                source_hash = source_response_sha256(source.response["value"])
                if input_payload.get("source_response_sha256") != source_hash:
                    raise StudioError("planning_repair_source_mismatch")
                previous_hashes = []
                for previous_role in REPAIR_OPERATION_NAMES[: attempt - 1]:
                    previous = await self._operation(session, previous_role)
                    if (
                        previous is None
                        or previous.status != "ready"
                        or "value" not in (previous.response or {})
                    ):
                        raise StudioError("planning_repair_chain_unavailable")
                    previous_hashes.append(source_response_sha256(previous.response["value"]))
                if input_payload.get("prior_repair_response_sha256") != previous_hashes:
                    raise StudioError("planning_repair_chain_mismatch")
                repair_detail = {"source_role": source_role, "attempt": attempt}
            existing = await self._operation(session, operation_name)
            if existing is not None:
                if existing.input_hash != input_hash:
                    raise StudioError("planning_input_changed")
                if existing.status == "ready":
                    return existing.response["value"]
                run.status, run.error_code = "submission_unknown", "creative_submission_unknown"
                existing.status = "unknown"
                await self._mark_unknown_cost(session, profile, operation_name)
                await self.store.event(
                    session,
                    run,
                    run.status,
                    "creative_submission_unknown",
                    {
                        "role": operation_name,
                        "revision": self.revision,
                        **repair_detail,
                    },
                )
                unknown = True
            else:
                context = await self._operation(session, "prepare_context")
                if context is None:
                    raise StudioError("planning_context_missing")
                seconds = min(
                    (context.deadline_at - _utc_now()).total_seconds(), limits["deadline_seconds"]
                )
                if seconds <= 0:
                    raise StudioError("planning_deadline_exceeded")
                if run.mode == "live":
                    await self._require_reservation(session, profile, operation_name)
                session.add(
                    StudioPlanningOperation(
                        run_id=self.run_id,
                        revision=self.revision,
                        role=operation_name,
                        input_hash=input_hash,
                        request=request,
                        status="submitting",
                        deadline_at=context.deadline_at,
                        expires_at=profile.expires_at,
                    )
                )
                await self.store.event(
                    session,
                    run,
                    "ideating",
                    "creative_step_started",
                    {
                        "role": operation_name,
                        "revision": self.revision,
                        **repair_detail,
                    },
                )
        if unknown:
            raise StudioError("creative_submission_unknown")
        try:
            async with asyncio.timeout(seconds):
                value = await producer()
            if not isinstance(value, (Mapping, str)):
                raise StudioError("planning_response_invalid")
            encoded = (
                value.encode()
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False).encode()
            )
            if len(encoded) > limits["max_output_bytes"]:
                raise StudioError("planning_output_limit")
        except BaseException:
            # Cancellation and transport errors cannot establish a free call.
            # If the worker lost its claim, its intent remains for the next owner.
            async with self.store.transaction() as session:
                row = await self._operation(session, operation_name)
                run = await session.get(StudioRun, self.run_id)
                if row is not None and row.status == "submitting":
                    row.status, row.error_code = "unknown", "creative_submission_unknown"
                    profile = await session.get(StudioPlannerRun, self.run_id)
                    if profile is not None:
                        await self._mark_unknown_cost(session, profile, operation_name)
                if run is not None and run.status == "ideating" and run.revision == self.revision:
                    run.status, run.error_code = "submission_unknown", "creative_submission_unknown"
                    await self.store.event(
                        session,
                        run,
                        run.status,
                        "creative_submission_unknown",
                        {
                            "role": operation_name,
                            "revision": self.revision,
                        },
                    )
            raise
        async with self.store.transaction() as session:
            row = await self._operation(session, operation_name)
            run = await session.get(StudioRun, self.run_id)
            if row is None or row.status != "submitting":
                raise StudioError("planning_receipt_conflict")
            row.response, row.status, row.completed_at = {"value": value}, "ready", _utc_now()
            if run is not None and run.status == "ideating" and run.revision == self.revision:
                await self.store.event(
                    session,
                    run,
                    "ideating",
                    "creative_step_completed",
                    {
                        "role": operation_name,
                        "revision": self.revision,
                        **repair_detail,
                    },
                )
        return value

    async def creative_step_validated(self, operation_name: str) -> None:
        """Record one semantic-validation milestone for a ready creative receipt."""
        async with self.store.transaction() as session:
            run = await session.get(StudioRun, self.run_id)
            operation = await self._operation(session, operation_name)
            if (
                run is None
                or run.status != "ideating"
                or run.revision != self.revision
                or operation is None
                or operation.status != "ready"
            ):
                return
            detail: dict[str, Any] = {
                "role": operation_name,
                "revision": self.revision,
            }
            metadata = operation.request.get("metadata")
            if operation_name in REPAIR_OPERATION_NAMES and isinstance(metadata, dict):
                source_role = metadata.get("source_role")
                attempt = metadata.get("attempt")
                if isinstance(source_role, str) and type(attempt) is int:
                    detail.update(source_role=source_role, attempt=attempt)
            existing = (
                await session.scalars(
                    select(StudioEvent).where(
                        StudioEvent.run_id == self.run_id,
                        StudioEvent.event_type == "creative_step_validated",
                    )
                )
            ).all()
            if any(
                event.detail.get("role") == operation_name
                and event.detail.get("revision") == self.revision
                for event in existing
            ):
                return
            await self.store.event(
                session,
                run,
                "ideating",
                "creative_step_validated",
                detail,
            )

    async def _require_reservation(
        self, session: Any, profile: StudioPlannerRun, role: str
    ) -> None:
        stage = "ideation" if role in (*PLANNING_ROLES, *REPAIR_OPERATION_NAMES) else role
        key = (
            f"revision:{self.revision}:role:{role}"
            if role in (*PLANNING_ROLES, *REPAIR_OPERATION_NAMES)
            else "initial" if self.revision == 1 else f"revision:{self.revision - 1}"
        )
        entry = await session.scalar(
            select(StudioCostLedger).where(
                StudioCostLedger.run_id == self.run_id,
                StudioCostLedger.profile_version == profile.configuration["cost_profile_version"],
                StudioCostLedger.stage == stage,
                StudioCostLedger.operation_key == key,
            )
        )
        if entry is None or entry.cost_state != "reserved" or entry.reserved_usd <= 0:
            raise StudioError("planning_reservation_missing")

    async def _mark_unknown_cost(self, session: Any, profile: StudioPlannerRun, role: str) -> None:
        stage = "ideation" if role in (*PLANNING_ROLES, *REPAIR_OPERATION_NAMES) else role
        key = (
            f"revision:{self.revision}:role:{role}"
            if role in (*PLANNING_ROLES, *REPAIR_OPERATION_NAMES)
            else "initial" if self.revision == 1 else f"revision:{self.revision - 1}"
        )
        await session.execute(
            update(StudioCostLedger)
            .where(
                StudioCostLedger.run_id == self.run_id,
                StudioCostLedger.profile_version == profile.configuration["cost_profile_version"],
                StudioCostLedger.stage == stage,
                StudioCostLedger.operation_key == key,
                StudioCostLedger.cost_state == "reserved",
            )
            .values(cost_state="submission_unknown", reconciled_at=_utc_now())
        )
