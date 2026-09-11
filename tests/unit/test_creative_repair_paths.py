"""Full planner repair paths, using the production planning graph with fake receipts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from langgraph.errors import NodeCancelledError

from myloware.storage.studio_store import StudioError
from myloware.studio.creative_planning import (
    CreativePlanner,
    CreativePlanningContext,
)
from myloware.studio.creative_repair import CreativeRepairError, source_response_sha256
from tests.unit.test_creative_planning import _Callback


class _FaultingOperation:
    """Wrap valid baseline responses and make one bounded repair response per fault."""

    def __init__(self, faults: Mapping[str, Callable[[dict[str, Any]], None]] = {}) -> None:
        self.base = _Callback()
        self.faults = dict(faults)
        self.repair_calls: list[dict[str, Any]] = []
        self.repair_responses: list[
            Mapping[str, Any] | BaseException | Callable[[Mapping[str, Any]], Mapping[str, Any]]
        ] = []
        self.source_outputs: dict[str, dict[str, Any]] = {}

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
    ) -> Mapping[str, Any]:
        if operation_name.startswith("repair_"):
            call = {
                "role": operation_name,
                "payload": deepcopy(dict(input_payload)),
                "metadata": dict(metadata),
            }
            self.repair_calls.append(call)
            response = self.repair_responses.pop(0) if self.repair_responses else self._patch(call)
            if isinstance(response, BaseException):
                raise response
            if callable(response):
                response = response(call)
            return dict(response)
        if operation_name == "explore_object":
            # The surreal completion arrives first. Repair order must still be object, then surreal.
            await asyncio.sleep(0.02)
        result = dict(
            await self.base(
                operation_name=operation_name,
                input_payload=input_payload,
                response_schema=response_schema,
                model=model,
                prompt=prompt,
                limits=limits,
                metadata=metadata,
                producer=producer,
            )
        )
        fault = self.faults.get(operation_name)
        if fault is not None:
            fault(result)
        self.source_outputs[operation_name] = deepcopy(result)
        return result

    @staticmethod
    def _patch(call: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = call["payload"]
        source_role = payload["source_role"]
        patches: list[dict[str, str]] = []
        for target in payload["allowed_targets"]:
            if target.endswith(".material"):
                value = "repaired material"
            elif target.endswith(".action"):
                value = "The literal material visibly completes one clear action before settling."
            elif target.endswith(".candidate_id"):
                value = payload["context"]["offered_candidate_ids"][0]
            elif target.endswith(".title_noun"):
                value = "Teacup"
            else:
                raise AssertionError(f"unexpected repair target: {target}")
            patches.append({"target": target, "value": value})
        return {
            "source_role": source_role,
            "mode": "patch",
            "patches": patches,
            "replacement": None,
        }


async def _plan(operation: _FaultingOperation, *, item: str = "teacup", repair_attempts: int = 2):
    return await CreativePlanner(object(), model="test-model").create_plan(
        run_id=uuid4(),
        revision=1,
        item=item,
        context=CreativePlanningContext(),
        run_operation=operation,
        repair_attempts=repair_attempts,
    )


@pytest.mark.asyncio
async def test_explorer_repairs_follow_role_order_not_reversed_completion_order() -> None:
    def bad_object(result: dict[str, Any]) -> None:
        result["cards"][0].pop("material")

    def bad_surreal(result: dict[str, Any]) -> None:
        result["cards"][0].pop("action")

    operation = _FaultingOperation({"explore_object": bad_object, "explore_surreal": bad_surreal})
    plan = await _plan(operation)

    assert len(plan.scenes) == 12
    assert [call["role"] for call in operation.repair_calls] == ["repair_1", "repair_2"]
    assert [call["payload"]["source_role"] for call in operation.repair_calls] == [
        "explore_object",
        "explore_surreal",
    ]


@pytest.mark.asyncio
async def test_curator_unknown_id_repairs_only_the_candidate_id_leaf() -> None:
    def unknown_id(result: dict[str, Any]) -> None:
        result["ranked"][0]["candidate_id"] = "missing_candidate"

    operation = _FaultingOperation({"curate": unknown_id})
    plan = await _plan(operation)

    assert len(plan.scenes) == 12
    repair = operation.repair_calls[0]
    assert repair["payload"]["source_role"] == "curate"
    assert repair["payload"]["allowed_targets"] == ["records[0].candidate_id"]
    assert (
        repair["payload"]["original_output"]["ranked"][1:]
        == operation.source_outputs["curate"]["ranked"][1:]
    )


@pytest.mark.asyncio
async def test_writer_punctuation_only_action_repairs_the_action_leaf_and_preserves_siblings() -> (
    None
):
    def punctuation_action(result: dict[str, Any]) -> None:
        result["shots"][0]["storyboard"]["action"] = "!!!"

    operation = _FaultingOperation({"write_shots": punctuation_action})
    plan = await _plan(operation)

    assert len(plan.scenes) == 12
    repair = operation.repair_calls[0]
    assert repair["payload"]["source_role"] == "write_shots"
    assert repair["payload"]["allowed_targets"] == ["records[0].storyboard.action"]
    assert (
        repair["payload"]["original_output"]["shots"][1:]
        == operation.source_outputs["write_shots"]["shots"][1:]
    )


@pytest.mark.asyncio
async def test_writer_repairs_all_invalid_nouns_without_touching_storyboards() -> None:
    def bad_nouns(result: dict[str, Any]) -> None:
        for shot in result["shots"]:
            shot["title_noun"] = "Cup"

    operation = _FaultingOperation({"write_shots": bad_nouns})
    plan = await _plan(operation)

    assert len(plan.scenes) == 12
    repair = operation.repair_calls[0]
    assert repair["payload"]["allowed_targets"] == sorted(
        f"records[{index}].title_noun" for index in range(12)
    )
    assert all(target.endswith(".title_noun") for target in repair["payload"]["allowed_targets"])
    assert all(scene.title.endswith("Teacup") for scene in plan.scenes)


@pytest.mark.asyncio
async def test_invalid_first_repair_uses_second_global_slot_then_succeeds() -> None:
    def bad_noun(result: dict[str, Any]) -> None:
        result["shots"][0]["title_noun"] = "Cup"

    operation = _FaultingOperation({"write_shots": bad_noun})
    operation.repair_responses = [
        {
            "source_role": "write_shots",
            "mode": "patch",
            "patches": [{"target": "records[0].not_allowed", "value": "no"}],
            "replacement": None,
        }
    ]
    plan = await _plan(operation)

    assert len(plan.scenes) == 12
    assert [call["role"] for call in operation.repair_calls] == ["repair_1", "repair_2"]
    assert plan.scenes[0].title.endswith("Teacup")


@pytest.mark.asyncio
async def test_two_invalid_repairs_stop_without_a_third_provider_call() -> None:
    def bad_noun(result: dict[str, Any]) -> None:
        result["shots"][0]["title_noun"] = "Cup"

    operation = _FaultingOperation({"write_shots": bad_noun})
    operation.repair_responses = [
        {
            "source_role": "write_shots",
            "mode": "patch",
            "patches": [{"target": "records[0].not_allowed", "value": "no"}],
            "replacement": None,
        },
        {
            "source_role": "write_shots",
            "mode": "patch",
            "patches": [{"target": "records[0].also_not_allowed", "value": "no"}],
            "replacement": None,
        },
    ]

    with pytest.raises(CreativeRepairError, match="creative_repair_exhausted"):
        await _plan(operation)
    assert [call["role"] for call in operation.repair_calls] == ["repair_1", "repair_2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (RuntimeError("provider failed"), RuntimeError),
        (asyncio.CancelledError(), NodeCancelledError),
    ],
)
async def test_repair_provider_failures_propagate_without_consuming_repair_two(
    failure: BaseException,
    expected: type[BaseException],
) -> None:
    def bad_noun(result: dict[str, Any]) -> None:
        result["shots"][0]["title_noun"] = "Cup"

    operation = _FaultingOperation({"write_shots": bad_noun})
    operation.repair_responses = [failure]
    with pytest.raises(expected):
        await _plan(operation)
    assert [call["role"] for call in operation.repair_calls] == ["repair_1"]


@pytest.mark.asyncio
async def test_repair_studio_error_propagates_without_consuming_repair_two() -> None:
    def bad_noun(result: dict[str, Any]) -> None:
        result["shots"][0]["title_noun"] = "Cup"

    operation = _FaultingOperation({"write_shots": bad_noun})
    operation.repair_responses = [StudioError("planning_reservation_missing")]
    with pytest.raises(StudioError, match="planning_reservation_missing"):
        await _plan(operation)
    assert [call["role"] for call in operation.repair_calls] == ["repair_1"]


@pytest.mark.asyncio
async def test_valid_short_custom_noun_uses_no_repair() -> None:
    operation = _FaultingOperation()
    plan = await _plan(operation, item="tiny antique teacup")

    assert len(plan.scenes) == 12
    assert all(scene.title.endswith("tiny antique teacup") for scene in plan.scenes)
    assert operation.repair_calls == []


@pytest.mark.asyncio
async def test_writer_missing_middle_record_uses_one_append_and_preserves_the_other_eleven() -> (
    None
):
    saved: dict[str, dict[str, Any]] = {}

    def omit_middle(result: dict[str, Any]) -> None:
        saved["record"] = result["shots"].pop(5)

    operation = _FaultingOperation({"write_shots": omit_middle})
    operation.repair_responses = [
        lambda call: {
            "source_role": "write_shots",
            "mode": "append",
            "patches": [],
            "append": saved["record"],
            "replacement": None,
        }
    ]
    plan = await _plan(operation)

    assert len(plan.scenes) == 12
    repair = operation.repair_calls[0]
    assert repair["payload"]["source_role"] == "write_shots"
    assert repair["payload"]["allowed_targets"] == []
    assert (
        repair["payload"]["original_output"]["shots"]
        == operation.source_outputs["write_shots"]["shots"]
    )
    assert plan.scenes[5].title == "Material6 Teacup"
    assert [scene.title for scene in plan.scenes if scene.ordinal != 6] == [
        f"Material{ordinal} Teacup" for ordinal in range(1, 13) if ordinal != 6
    ]


@pytest.mark.asyncio
async def test_two_missing_writer_records_consume_two_append_slots_and_keep_first_append() -> None:
    saved: list[dict[str, Any]] = []

    def omit_two(result: dict[str, Any]) -> None:
        saved.extend([result["shots"].pop(8), result["shots"].pop(4)])

    operation = _FaultingOperation({"write_shots": omit_two})
    operation.repair_responses = [
        lambda _call: {
            "source_role": "write_shots",
            "mode": "append",
            "patches": [],
            "append": saved[0],
            "replacement": None,
        },
        lambda _call: {
            "source_role": "write_shots",
            "mode": "append",
            "patches": [],
            "append": saved[1],
            "replacement": None,
        },
    ]
    plan = await _plan(operation)

    assert len(plan.scenes) == 12
    assert [call["role"] for call in operation.repair_calls] == ["repair_1", "repair_2"]
    assert all(scene.title == f"Material{scene.ordinal} Teacup" for scene in plan.scenes)


@pytest.mark.asyncio
async def test_append_when_not_authorized_is_rejected_without_mutating_existing_records() -> None:
    def bad_noun(result: dict[str, Any]) -> None:
        result["shots"][0]["title_noun"] = "Cup"

    operation = _FaultingOperation({"write_shots": bad_noun})
    operation.repair_responses = [
        lambda call: {
            "source_role": "write_shots",
            "mode": "append",
            "patches": [],
            "append": deepcopy(call["payload"]["original_output"]["shots"][1]),
            "replacement": None,
        }
    ]
    plan = await _plan(operation)

    assert len(plan.scenes) == 12
    assert [call["role"] for call in operation.repair_calls] == ["repair_1", "repair_2"]
    assert operation.repair_calls[0]["payload"]["allowed_targets"] == ["records[0].title_noun"]
    assert (
        operation.repair_calls[0]["payload"]["original_output"]
        == operation.repair_calls[1]["payload"]["original_output"]
    )
    assert plan.scenes[0].title == "Material1 Teacup"


def test_source_response_hash_canonicalizes_valid_json_text_like_a_mapping() -> None:
    value = {"title": "Teacup", "ordinal": 1}
    assert source_response_sha256('{"ordinal":1,"title":"Teacup"}') == source_response_sha256(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("modifier", ["Very Quiet", "Quiet!", "   "])
async def test_invalid_modifier_repairs_only_modifier(modifier: str) -> None:
    operation = _FaultingOperation(
        {"write_shots": lambda value: value["shots"][0].update(title_modifier=modifier)}
    )
    operation.repair_responses = [
        {
            "source_role": "write_shots",
            "mode": "patch",
            "patches": [{"target": "records[0].title_modifier", "value": "Shifting"}],
            "append": None,
            "replacement": None,
        }
    ]
    plan = await _plan(operation)
    assert plan.scenes[0].title == "Shifting Teacup"
    assert operation.repair_calls[0]["payload"]["allowed_targets"] == ["records[0].title_modifier"]
    assert len(operation.repair_calls) == 1


@pytest.mark.asyncio
async def test_preflight_repair_failure_is_not_retried(monkeypatch) -> None:
    from myloware.studio.creative_planning import _PlanningRuntime

    calls = 0

    async def reject(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("preflight failure was retried")
        raise CreativeRepairError("repair_input_exceeded")

    monkeypatch.setattr(_PlanningRuntime, "_repair_invalid_result", reject)
    operation = _FaultingOperation(
        {"write_shots": lambda value: value["shots"][0].update(title_noun="Handle")}
    )
    with pytest.raises(CreativeRepairError, match="repair_input_exceeded"):
        await _plan(operation)
    assert calls == 1
    assert operation.repair_calls == []
