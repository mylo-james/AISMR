from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import pytest

from myloware.studio.creative_planning import (
    CreativePlanner,
    CreativePlanningContext,
    _Cards,
    _PlanningRuntime,
    _Selection,
)
from myloware.studio.creative_repair import (
    REPAIR_OPERATION_NAMES,
    CreativeRepairError,
    RepairPatch,
    apply_patches,
    source_response_sha256,
)


def _cards() -> list[dict[str, str]]:
    return [
        {
            "candidate_id": f"object_{index}",
            "material": f"material {index}",
            "action": f"action {index}",
            "setting": f"setting {index}",
            "hook": f"hook {index}",
            "payoff": f"payoff {index}",
            "object_fit": "Recognizable object.",
        }
        for index in range(15)
    ]


def test_patch_merges_only_the_invalid_nested_field() -> None:
    original = {"records": [{"storyboard": {"action": "old", "camera": "keep"}}]}
    patched = apply_patches(
        original,
        allowed_targets={"records[0].storyboard.action"},
        patches=(RepairPatch(target="records[0].storyboard.action", value="new"),),
    )
    assert patched["records"][0]["storyboard"] == {"action": "new", "camera": "keep"}
    assert source_response_sha256({"b": 2, "a": 1}) == source_response_sha256({"a": 1, "b": 2})
    with pytest.raises(CreativeRepairError, match="repair_unauthorized_patch_target"):
        apply_patches(
            original,
            allowed_targets={"records[0].storyboard.action"},
            patches=(RepairPatch(target="records[0].storyboard.camera", value="changed"),),
        )


@pytest.mark.asyncio
async def test_received_structural_output_repairs_only_the_invalid_card_field() -> None:
    calls: list[dict[str, Any]] = []
    source = {"cards": _cards()}
    source["cards"][0].pop("material")

    async def operation(**kwargs: Any) -> Mapping[str, Any] | str:
        calls.append(kwargs)
        if kwargs["operation_name"] == "explore_object":
            return source
        assert kwargs["operation_name"] == REPAIR_OPERATION_NAMES[0]
        payload = kwargs["input_payload"]
        assert payload["source_role"] == "explore_object"
        assert payload["source_response_sha256"] == source_response_sha256(source)
        assert payload["attempt"] == 1
        assert payload["allowed_targets"] == ["records[0].material"]
        return {
            "source_role": "explore_object",
            "mode": "patch",
            "patches": [{"target": "records[0].material", "value": "repaired material"}],
            "replacement": None,
        }

    runtime = _PlanningRuntime(
        planner=CreativePlanner(object(), model="test-model"),
        run_id=uuid4(),
        revision=1,
        item="Teacup",
        context=CreativePlanningContext(),
        replacement_ordinals=tuple(range(1, 13)),
        run_operation=operation,
        repair_attempts=1,
    )
    # Explorer calls may finish in either order. Invalid results are deliberately
    # deferred to the join node, where source-role order assigns repair_1/repair_2.
    result = await runtime.call("explore_object", {"cards": []}, _Cards, "test")
    assert result == {}
    joined = await runtime.join_candidates({"object_cards": (), "surreal_cards": ()})
    assert joined["candidates"][0].material == "repaired material"
    assert joined["candidates"][1].model_dump() == source["cards"][1]
    repair = calls[1]
    assert repair["metadata"]["source_role"] == "explore_object"
    assert repair["metadata"]["attempt"] == 1


@pytest.mark.asyncio
async def test_all_invalid_leaves_still_require_patches_not_role_replacement() -> None:
    calls: list[dict[str, Any]] = []
    source = {"cards": _cards()}
    for card in source["cards"]:
        card.pop("material")

    async def operation(**kwargs: Any) -> Mapping[str, Any] | str:
        calls.append(kwargs)
        if kwargs["operation_name"] == "explore_object":
            return source
        assert kwargs["operation_name"] == "repair_1"
        assert kwargs["response_schema"]["schema"]["properties"]["mode"]["enum"] == ["patch"]
        return {
            "source_role": "explore_object",
            "mode": "patch",
            "patches": [
                {"target": f"records[{index}].material", "value": f"fixed {index}"}
                for index in range(15)
            ],
            "replacement": None,
        }

    runtime = _PlanningRuntime(
        planner=CreativePlanner(object(), model="test-model"),
        run_id=uuid4(),
        revision=1,
        item="Teacup",
        context=CreativePlanningContext(),
        replacement_ordinals=tuple(range(1, 13)),
        run_operation=operation,
        repair_attempts=1,
    )
    await runtime.call("explore_object", {"cards": []}, _Cards, "test")
    joined = await runtime.join_candidates({"object_cards": (), "surreal_cards": ()})
    repaired = joined["candidates"]
    assert [card.material for card in repaired] == [f"fixed {index}" for index in range(15)]
    assert [card.action for card in repaired] == [f"action {index}" for index in range(15)]


@pytest.mark.asyncio
async def test_curator_unknown_id_repairs_the_identity_leaf() -> None:
    cards = _cards()

    async def operation(**kwargs: Any) -> Mapping[str, Any] | str:
        if kwargs["operation_name"] == "curate":
            return {"ranked": [{"candidate_id": "candidate_03", "reason": "clear event"}]}
        assert kwargs["operation_name"] == "repair_1"
        assert kwargs["input_payload"]["allowed_targets"] == ["records[0].candidate_id"]
        return {
            "source_role": "curate",
            "mode": "patch",
            "patches": [{"target": "records[0].candidate_id", "value": "object_3"}],
            "replacement": None,
        }

    runtime = _PlanningRuntime(
        planner=CreativePlanner(object(), model="test-model"),
        run_id=uuid4(),
        revision=1,
        item="Teacup",
        context=CreativePlanningContext(),
        replacement_ordinals=(1,),
        run_operation=operation,
        repair_attempts=1,
    )
    result = await runtime.call(
        "curate", {"cards": cards, "replacement_ordinals": (1,)}, _Selection, "test"
    )
    assert result["ranked"][0]["candidate_id"] == "object_3"
