from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from myloware.workflows.monthly import deterministic_fixture_plan, monthly_plan_digest
from myloware.workflows.scenes import SceneIdea, ScenePlan, deterministic_scene_plan, parse_plan


def test_scene_plan_is_ordered_scene_only_and_has_read_only_compatibility_views() -> None:
    plan = deterministic_scene_plan(uuid4(), "teacup")

    dumped = plan.model_dump(mode="json")
    assert dumped["schema_version"] == 2
    assert "ideas" not in dumped
    assert all(set(scene) == {"ordinal", "title", "visual_prompt"} for scene in dumped["scenes"])
    assert plan.ideas == plan.scenes
    assert plan.scenes[0].label == plan.scenes[0].title
    assert [scene.ordinal for scene in plan.scenes] == list(range(1, 13))


@pytest.mark.parametrize(
    "payload",
    [
        {"ordinal": 1, "title": "Frozen Teacup", "visual_prompt": "Prompt.", "month": "January"},
        {"ordinal": 1, "title": "Frozen Teacup", "visual_prompt": "Prompt.", "spoken_text": "No."},
    ],
)
def test_scene_idea_rejects_calendar_and_spoken_text(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SceneIdea.model_validate(payload)


def test_parse_plan_dispatches_strictly_and_keeps_v1_digest_unchanged() -> None:
    v1 = deterministic_fixture_plan(uuid4(), "teacup")
    v2 = deterministic_scene_plan(uuid4(), "teacup")

    assert parse_plan(v1.model_dump(mode="json")) == v1
    assert parse_plan(v2.model_dump(mode="json")) == v2
    assert monthly_plan_digest(parse_plan(v1.model_dump(mode="json"))) == monthly_plan_digest(v1)
    with pytest.raises(ValueError, match="schema_version"):
        parse_plan({"schema_version": 3})


def test_scene_plan_rejects_gaps_and_repeated_ordinals() -> None:
    plan = deterministic_scene_plan(uuid4(), "teacup")
    scenes = list(plan.scenes)
    scenes[1] = scenes[1].model_copy(update={"ordinal": 1})
    with pytest.raises(ValidationError, match="ordered ordinals"):
        ScenePlan.model_validate(plan.model_dump() | {"scenes": scenes})


@pytest.mark.parametrize("title", ["Frozen Crystal", "Velvet Chair", "Floating"])
def test_scene_title_cannot_replace_the_selected_object(title: str) -> None:
    plan = deterministic_scene_plan(uuid4(), "teacup").model_dump()
    plan["scenes"][0]["title"] = title
    with pytest.raises(ValidationError):
        ScenePlan.model_validate(plan)


def test_custom_object_can_use_a_shorter_grounded_noun() -> None:
    plan = deterministic_scene_plan(uuid4(), "tiny antique teacup").model_dump()
    plan["scenes"][0]["title"] = "Shifting Teacup"
    assert ScenePlan.model_validate(plan).scenes[0].title == "Shifting Teacup"
