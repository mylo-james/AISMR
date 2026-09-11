"""Receipt and replay boundaries for bounded creative-plan repairs.

These tests intentionally exercise ``DurablePlanning`` directly.  The creative
planner owns deciding that a candidate needs repair; this layer owns the
immutable receipt, reservation, and no-resubmit guarantees after that decision.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.config.studio import StudioSettings
from myloware.storage.models import Base, _utc_now
from myloware.storage.studio_models import (
    StudioEvent,
    StudioPlannerRun,
    StudioPlanningOperation,
    StudioRun,
)
from myloware.storage.studio_store import StudioError, StudioStore, digest
from myloware.studio.creative_planning import (
    PLANNING_ROLE_LIMITS,
    CreativePlanner,
    CreativePlanningContext,
)
from myloware.studio.creative_repair import source_response_sha256
from myloware.studio.planning_store import DurablePlanning, planning_contract


def _settings(*, repair_attempts: int) -> StudioSettings:
    return StudioSettings(
        enabled=True,
        mode="live",
        live_enabled=True,
        ideation_backend="openai",
        openai_key="test-key",
        fal_key="test-key",
        render_real=True,
        session_secret="x" * 32,
        daily_budget_usd=Decimal(10000),
        run_reservation_usd=Decimal(1),
        cost_profile_version="creative-profile-v2",
        cost_input_moderation_usd=Decimal(1),
        cost_ideation_usd=Decimal(1),
        cost_plan_moderation_usd=Decimal(1),
        cost_video_request_usd=Decimal(1),
        cost_narration_batch_usd=Decimal(1),
        cost_render_usd=Decimal(1),
        cost_final_moderation_usd=Decimal(1),
        plan_revisions=1,
        visitor_runs_24h=3,
        planner_version="creative-v2",
        creative_repair_attempts=repair_attempts,
        openai_ideation_model="creative-test-model",
    )


async def _store(  # type: ignore[no-untyped-def]
    tmp_path, *, repair_attempts: int = 2
) -> tuple[Any, StudioStore, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    store = StudioStore(
        async_sessionmaker(engine, expire_on_commit=False),
        _settings(repair_attempts=repair_attempts),
    )
    visitor, _ = await store.new_visitor("127.0.0.1")
    run_id = await store.admit(
        visitor_id=visitor.id,
        item_text="teacup",
        start_key="creative-repair-start",
        ip="127.0.0.1",
    )
    return engine, store, run_id


def _source_value() -> dict[str, object]:
    return {"shots": [{"ordinal": 1, "title_modifier": "Quiet", "title_noun": "Teacup"}]}


def _cards(prefix: str) -> list[dict[str, str]]:
    return [
        {
            "candidate_id": f"{prefix}_{index}",
            "material": f"{prefix} material {index}",
            "action": f"{prefix} action {index}",
            "setting": f"{prefix} setting {index}",
            "hook": "A visible impossible transformation begins.",
            "payoff": "The object settles into a readable final form.",
            "object_fit": "The teacup stays recognizable throughout.",
        }
        for index in range(1, 16)
    ]


class _RepairingCompletions:
    """Deterministic provider: one invalid writer noun, then a scalar repair patch."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.writer_output: dict[str, object] | None = None

    async def create(self, **kwargs: Any) -> Any:
        name = kwargs["response_format"]["json_schema"]["name"]
        role = name.removeprefix("aismr_").rsplit("_v", 1)[0]
        payload = json.loads(kwargs["messages"][1]["content"])
        self.calls.append(role)
        if role in {"explore_object", "explore_surreal", "replenish"}:
            result: dict[str, object] = {"cards": _cards(role.removeprefix("explore_"))}
        elif role in {"curate", "recurate"}:
            result = {
                "ranked": [
                    {"candidate_id": card["candidate_id"], "reason": "distinct physical action"}
                    for card in payload["cards"][: len(payload["replacement_ordinals"])]
                ]
            }
        elif role == "write_shots":
            cards = {card["candidate_id"]: card for card in payload["cards"]}
            shots: list[dict[str, object]] = []
            for chosen in payload["selected"]:
                ordinal = chosen["ordinal"]
                card = cards[chosen["candidate_id"]]
                shots.append(
                    {
                        "ordinal": ordinal,
                        "candidate_id": chosen["candidate_id"],
                        "title_modifier": f"Shifting{ordinal}",
                        "title_noun": "Cup" if ordinal == 1 else "Teacup",
                        "storyboard": {
                            "opening_frame": "A recognizable teacup rests on a stone ledge with its handle facing the camera.",
                            "action": f"The teacup visibly performs {card['action']} from rim to base while its handle remains in view.",
                            "payoff_frame": "The changed rim settles into a complete ring and the teacup remains upright.",
                            "camera": "Locked three-quarter close-up keeps handle and base visible.",
                            "light_and_texture": "Low side light reveals texture and a crisp contact shadow.",
                        },
                    }
                )
            result = {"shots": shots}
            self.writer_output = result
        elif role == "repair_1":
            result = {
                "source_role": "write_shots",
                "mode": "patch",
                "patches": [{"target": "records[0].title_noun", "value": "Teacup"}],
                "replacement": None,
            }
        else:
            raise AssertionError(f"unexpected role: {role}")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))]
        )


async def _seed_ready_source(  # type: ignore[no-untyped-def]
    store: StudioStore, run_id, value: object
) -> None:
    async with store.transaction() as session:
        profile = await session.get(StudioPlannerRun, run_id)
        assert profile is not None
        session.add(
            StudioPlanningOperation(
                run_id=run_id,
                revision=1,
                role="write_shots",
                input_hash=digest({"source": "write_shots"}),
                request={"metadata": {"role": "write_shots"}},
                response={"value": value},
                status="ready",
                deadline_at=_utc_now() + timedelta(seconds=60),
                completed_at=_utc_now(),
                expires_at=profile.expires_at,
            )
        )


async def _prepare(store: StudioStore, run_id) -> DurablePlanning:  # type: ignore[no-untyped-def]
    operations = DurablePlanning(store, run_id, 1)
    await operations.context()
    await _seed_ready_source(store, run_id, _source_value())
    return operations


async def _seed_repaired_curation(  # type: ignore[no-untyped-def]
    store: StudioStore,
    run_id,
    *,
    repair_source_role: str = "curate",
    offered_mutator: Any | None = None,
    selected_mutator: Any | None = None,
) -> None:
    """Persist an invalid curator receipt and the later writer-bound correction evidence."""
    cards = _cards("history")[:12]
    ordinals = list(range(1, 13))
    curate_payload = {"cards": cards, "replacement_ordinals": ordinals}
    curate_request = {
        "input_payload": curate_payload,
        "metadata": {"schema_version": 5},
    }
    curate_response = {"ranked": [{"candidate_id": "missing_card", "reason": "invalid"}]}
    offered = json.loads(json.dumps(cards))
    selected = [
        {"ordinal": ordinal, "candidate_id": card["candidate_id"], "reason": "accepted"}
        for ordinal, card in zip(ordinals, cards, strict=True)
    ]
    if offered_mutator is not None:
        offered_mutator(offered)
    if selected_mutator is not None:
        selected_mutator(selected)
    writer_request = {"input_payload": {"cards": offered, "selected": selected}}
    repair_request = {
        "input_payload": {
            "source_role": repair_source_role,
            "source_response_sha256": source_response_sha256(curate_response),
        }
    }
    async with store.transaction() as session:
        profile = await session.get(StudioPlannerRun, run_id)
        assert profile is not None
        deadline = _utc_now() + timedelta(seconds=60)
        session.add_all(
            [
                StudioPlanningOperation(
                    run_id=run_id,
                    revision=1,
                    role="curate",
                    input_hash=digest(curate_request),
                    request=curate_request,
                    response={"value": curate_response},
                    status="ready",
                    deadline_at=deadline,
                    completed_at=_utc_now(),
                    expires_at=profile.expires_at,
                ),
                StudioPlanningOperation(
                    run_id=run_id,
                    revision=1,
                    role="repair_1",
                    input_hash=digest(repair_request),
                    request=repair_request,
                    response={"value": {"source_role": "curate", "mode": "patch"}},
                    status="ready",
                    deadline_at=deadline,
                    completed_at=_utc_now(),
                    expires_at=profile.expires_at,
                ),
                StudioPlanningOperation(
                    run_id=run_id,
                    revision=1,
                    role="write_shots",
                    input_hash=digest(writer_request),
                    request=writer_request,
                    response={"value": {"shots": []}},
                    status="ready",
                    deadline_at=deadline,
                    completed_at=_utc_now(),
                    expires_at=profile.expires_at,
                ),
            ]
        )


def _repair_kwargs(  # type: ignore[no-untyped-def]
    run_id,
    *,
    attempt: int,
    issues: list[str],
    prior: list[str] | None = None,
) -> dict[str, object]:
    source = _source_value()
    source_hash = source_response_sha256(source)
    role = f"repair_{attempt}"
    input_payload: dict[str, object] = {
        "source_role": "write_shots",
        "source_response_sha256": source_hash,
        "attempt": attempt,
        "issues": [
            {
                "source_role": "write_shots",
                "field": issue,
                "rule": "validation_failed",
                "message": "Repair this validated storyboard field.",
            }
            for issue in issues
        ],
        "prior_repair_response_sha256": prior or [],
    }
    return {
        "operation_name": role,
        "input_payload": input_payload,
        "response_schema": {},
        "model": "creative-test-model",
        "prompt": {"system": "repair", "user": "repair the saved candidate"},
        "limits": {**PLANNING_ROLE_LIMITS["write_shots"], "deadline_seconds": 30},
        "metadata": {
            "profile_version": "creative-v2",
            "run_id": str(run_id),
            "revision": 1,
            "prompt_version": "creative-repair-v1",
            "schema_version": 1,
            "operation_key": f"{run_id}:1:{role}",
            "input_sha256": sha256(
                json.dumps(input_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "source_role": "write_shots",
            "attempt": attempt,
        },
    }


@pytest.mark.asyncio
async def test_repair_one_persists_provenance_and_replays_without_resubmitting(tmp_path) -> None:
    engine, store, run_id = await _store(tmp_path)
    try:
        operations = await _prepare(store, run_id)
        kwargs = _repair_kwargs(run_id, attempt=1, issues=["duplicate_action"])
        calls = 0

        async def producer() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"shots": ["repaired"]}

        assert await operations(**kwargs, producer=producer) == {"shots": ["repaired"]}
        assert await DurablePlanning(store, run_id, 1)(**kwargs, producer=producer) == {
            "shots": ["repaired"]
        }
        assert calls == 1
        async with store.factory() as session:
            row = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == run_id,
                    StudioPlanningOperation.revision == 1,
                    StudioPlanningOperation.role == "repair_1",
                )
            )
            assert row is not None and row.status == "ready"
            assert row.request["metadata"]["source_role"] == "write_shots"
            assert row.request["metadata"]["attempt"] == 1
            actual_source_hash = row.request["input_payload"]["source_response_sha256"]
            assert actual_source_hash == source_response_sha256(_source_value())
            assert row.request["input_payload"]["issues"][0]["field"] == "duplicate_action"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repair_rejects_changed_input_and_tampered_source_hash_before_provider(
    tmp_path,
) -> None:
    engine, store, run_id = await _store(tmp_path)
    try:
        operations = await _prepare(store, run_id)
        kwargs = _repair_kwargs(run_id, attempt=1, issues=["duplicate_action"])
        calls = 0

        async def producer() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"shots": ["repaired"]}

        await operations(**kwargs, producer=producer)
        changed = _repair_kwargs(run_id, attempt=1, issues=["missing_payoff"])
        with pytest.raises(StudioError, match="planning_input_changed"):
            await operations(**changed, producer=producer)
        tampered = _repair_kwargs(run_id, attempt=2, issues=["missing_payoff"])
        tampered["input_payload"] = {
            **tampered["input_payload"],  # type: ignore[arg-type]
            "source_response_sha256": "0" * 64,
        }
        tampered["metadata"] = {
            **tampered["metadata"],  # type: ignore[arg-type]
            "input_sha256": sha256(
                json.dumps(
                    tampered["input_payload"], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
        }
        with pytest.raises(StudioError, match="repair"):
            await operations(**tampered, producer=producer)
        assert calls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repair_unknown_receipt_stops_without_second_repair_or_resubmit(tmp_path) -> None:
    engine, store, run_id = await _store(tmp_path)
    try:
        operations = await _prepare(store, run_id)
        kwargs = _repair_kwargs(run_id, attempt=1, issues=["duplicate_action"])
        async with store.transaction() as session:
            profile = await session.get(StudioPlannerRun, run_id)
            assert profile is not None
            session.add(
                StudioPlanningOperation(
                    run_id=run_id,
                    revision=1,
                    role="repair_1",
                    input_hash=digest(
                        {key: value for key, value in kwargs.items() if key != "operation_name"}
                    ),
                    request={},
                    status="submitting",
                    deadline_at=_utc_now() + timedelta(seconds=30),
                    expires_at=profile.expires_at,
                )
            )
        calls = 0

        async def producer() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"shots": ["must not submit"]}

        with pytest.raises(StudioError, match="creative_submission_unknown"):
            await operations(**kwargs, producer=producer)
        with pytest.raises(StudioError):
            await operations(
                **_repair_kwargs(run_id, attempt=2, issues=["duplicate_action"]), producer=producer
            )
        assert calls == 0
        run = await store.get_run(run_id)
        assert run.status == "submission_unknown"
        async with store.factory() as session:
            rows = (
                await session.scalars(
                    select(StudioPlanningOperation).where(
                        StudioPlanningOperation.run_id == run_id,
                        StudioPlanningOperation.role.in_(["repair_1", "repair_2"]),
                    )
                )
            ).all()
            assert [(row.role, row.status) for row in rows] == [("repair_1", "unknown")]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repair_contract_is_opt_in_and_second_attempt_requires_ready_first_receipt(
    tmp_path,
) -> None:
    engine, store, run_id = await _store(tmp_path, repair_attempts=0)
    try:
        assert planning_contract() == planning_contract(repair_attempts=0)
        operations = DurablePlanning(store, run_id, 1)
        await operations.context()
        await _seed_ready_source(store, run_id, _source_value())
        calls = 0

        async def producer() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"shots": ["must not submit"]}

        with pytest.raises(StudioError, match="repair"):
            await operations(
                **_repair_kwargs(run_id, attempt=1, issues=["duplicate_action"]), producer=producer
            )
        assert calls == 0

        engine2, store2, run2 = await _store(tmp_path / "second")
        try:
            operations2 = await _prepare(store2, run2)
            with pytest.raises(StudioError, match="repair"):
                await operations2(
                    **_repair_kwargs(run2, attempt=2, issues=["duplicate_action"]),
                    producer=producer,
                )
            assert calls == 0
        finally:
            await engine2.dispose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repair_policy_caps_provider_effects_at_two_named_operations(tmp_path) -> None:
    engine, store, run_id = await _store(tmp_path)
    try:
        operations = await _prepare(store, run_id)
        calls = 0

        async def producer() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"shots": [f"repair {calls}"]}

        first = {"shots": ["repair 1"]}
        await operations(
            **_repair_kwargs(run_id, attempt=1, issues=["duplicate_action"]), producer=producer
        )
        await operations(
            **_repair_kwargs(
                run_id,
                attempt=2,
                issues=["missing_payoff"],
                prior=[source_response_sha256(first)],
            ),
            producer=producer,
        )
        with pytest.raises(StudioError, match="unknown_planning_role"):
            await operations(
                **_repair_kwargs(
                    run_id,
                    attempt=3,
                    issues=["missing_payoff"],
                    prior=[source_response_sha256(first)],
                ),
                producer=producer,
            )
        assert calls == 2
        async with store.factory() as session:
            rows = (
                await session.scalars(
                    select(StudioPlanningOperation.role).where(
                        StudioPlanningOperation.run_id == run_id,
                        StudioPlanningOperation.role.in_(["repair_1", "repair_2", "repair_3"]),
                    )
                )
            ).all()
            assert set(rows) == {"repair_1", "repair_2"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_plan_replays_a_durable_writer_title_repair_without_new_provider_calls(
    tmp_path,
) -> None:
    engine, store, run_id = await _store(tmp_path)
    try:
        operations = DurablePlanning(store, run_id, 1)
        await operations.context()
        completions = _RepairingCompletions()
        planner = CreativePlanner(
            SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            model="creative-test-model",
        )
        kwargs = {
            "run_id": run_id,
            "revision": 1,
            "item": "teacup",
            "context": CreativePlanningContext(),
            "repair_attempts": 2,
        }
        first = await planner.create_plan(run_operation=operations, **kwargs)
        second = await planner.create_plan(
            run_operation=DurablePlanning(store, run_id, 1), **kwargs
        )

        assert first == second
        assert first.scenes[0].title == "Shifting1 Teacup"
        assert completions.writer_output is not None
        expected_unchanged_titles = [
            f"{shot['title_modifier']} {shot['title_noun']}"
            for shot in completions.writer_output["shots"][1:]
        ]
        assert [scene.title for scene in first.scenes[1:]] == expected_unchanged_titles
        assert completions.calls.count("repair_1") == 1
        assert completions.calls.count("repair_2") == 0
        assert {role: completions.calls.count(role) for role in set(completions.calls)} == {
            "explore_object": 1,
            "explore_surreal": 1,
            "curate": 1,
            "write_shots": 1,
            "repair_1": 1,
        }
        async with store.factory() as session:
            repair = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == run_id,
                    StudioPlanningOperation.role == "repair_1",
                )
            )
            assert repair is not None and repair.status == "ready"
            assert repair.request["metadata"]["source_role"] == "write_shots"
            assert repair.request["metadata"]["attempt"] == 1
            events = (
                await session.scalars(
                    select(StudioEvent)
                    .where(
                        StudioEvent.run_id == run_id,
                        StudioEvent.event_type == "creative_step_validated",
                    )
                    .order_by(StudioEvent.sequence)
                )
            ).all()
            by_role = {event.detail["role"]: event.detail for event in events}
            assert len(events) == 5
            assert set(by_role) == {
                "explore_object",
                "explore_surreal",
                "curate",
                "write_shots",
                "repair_1",
            }
            assert all(event.detail["revision"] == 1 for event in events)
            assert by_role["repair_1"] == {
                "role": "repair_1",
                "revision": 1,
                "source_role": "write_shots",
                "attempt": 1,
            }
            assert [event.detail["role"] for event in events].index("write_shots") < [
                event.detail["role"] for event in events
            ].index("repair_1")
            timeline = (
                await session.scalars(
                    select(StudioEvent)
                    .where(StudioEvent.run_id == run_id)
                    .order_by(StudioEvent.sequence)
                )
            ).all()
            sequence = {
                (event.event_type, event.detail.get("role")): event.sequence for event in timeline
            }
            assert (
                sequence[("creative_step_completed", "write_shots")]
                < sequence[("creative_step_completed", "repair_1")]
                < sequence[("creative_step_validated", "write_shots")]
                < sequence[("creative_step_validated", "repair_1")]
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recent_keeps_writer_bound_repaired_curation_for_the_next_same_visitor_run(
    tmp_path,
) -> None:
    engine, store, first_run = await _store(tmp_path)
    try:
        await _seed_repaired_curation(store, first_run)
        first = await store.get_run(first_run)
        next_run = await store.admit(
            visitor_id=first.visitor_id,
            item_text="teacup",
            start_key="repaired-curation-history",
            ip="127.0.0.1",
        )
        context = await DurablePlanning(store, next_run, 1).context()
        assert len(context.recent_concepts) == 12
        assert [concept.material for concept in context.recent_concepts] == [
            f"history material {ordinal}" for ordinal in range(1, 13)
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repair_source_role", "offered_mutator", "selected_mutator"),
    [
        ("explore_object", None, None),
        (
            "curate",
            lambda offered: offered[0].update({"material": "altered material"}),
            None,
        ),
        (
            "curate",
            None,
            lambda selected: selected[0].update({"candidate_id": "missing_card"}),
        ),
    ],
    ids=("unrelated-source", "altered-offered-card", "writer-selected-id-not-offered"),
)
async def test_repaired_selection_rejects_unbound_correction_evidence(
    tmp_path,
    repair_source_role: str,
    offered_mutator: Any | None,
    selected_mutator: Any | None,
) -> None:
    engine, store, run_id = await _store(tmp_path)
    try:
        await _seed_repaired_curation(
            store,
            run_id,
            repair_source_role=repair_source_role,
            offered_mutator=offered_mutator,
            selected_mutator=selected_mutator,
        )
        operations = DurablePlanning(store, run_id, 1)
        async with store.factory() as session:
            record = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == run_id,
                    StudioPlanningOperation.role == "curate",
                )
            )
            run = await session.get(StudioRun, run_id)
            assert record is not None and run is not None
            request = record.request
            input_payload = request["input_payload"]
            cards = {card["candidate_id"]: card for card in input_payload["cards"]}
            assert (
                await operations._repaired_selection(session, record, request, input_payload, cards)
                is None
            )
    finally:
        await engine.dispose()
