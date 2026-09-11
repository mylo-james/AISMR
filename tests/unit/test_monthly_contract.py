from __future__ import annotations

from random import Random
from uuid import UUID

import pytest
from pydantic import ValidationError

from myloware.workflows.monthly import (
    CANONICAL_MONTHS,
    MONTHLY_CATALOG,
    RANDOM_ITEM_ID,
    MonthIdea,
    MonthlyPlan,
    deserialize_monthly_plan,
    deterministic_fixture_plan,
    display_monthly_plan_markdown,
    monthly_plan_digest,
    resolve_catalog_item,
    resolve_item,
    serialize_monthly_plan,
    validate_monthly_plan,
)

RUN_ID = UUID("12345678-1234-5678-1234-567812345678")


def _valid_payload() -> dict[str, object]:
    return deterministic_fixture_plan(RUN_ID, "swimming_pool").model_dump(mode="json")


def test_fixture_plan_is_canonical_and_deterministic() -> None:
    first = deterministic_fixture_plan(RUN_ID, "swimming_pool")
    second = deterministic_fixture_plan(str(RUN_ID), "swimming_pool")

    assert first == second
    assert first.item_id == "swimming_pool"
    assert [(idea.ordinal, idea.month) for idea in first.ideas] == list(
        enumerate(CANONICAL_MONTHS, start=1)
    )
    assert [idea.label for idea in first.ideas] == [
        "Frozen Swimming pool",
        "Crystal Swimming pool",
        "Velvet Swimming pool",
        "Moss Swimming pool",
        "Porcelain Swimming pool",
        "Amber Swimming pool",
        "Paper Swimming pool",
        "Cloud Swimming pool",
        "Obsidian Swimming pool",
        "Candlewax Swimming pool",
        "Moonstone Swimming pool",
        "Glass Swimming pool",
    ]
    assert len({idea.label for idea in first.ideas}) == len(first.ideas)
    assert len({idea.visual_prompt for idea in first.ideas}) == len(first.ideas)
    assert [idea.spoken_text for idea in first.ideas] == [
        f"{month}. {idea.label}." for month, idea in zip(CANONICAL_MONTHS, first.ideas, strict=True)
    ]
    assert all(len(idea.label) <= 48 for idea in first.ideas)
    assert all(len(idea.spoken_text) <= 80 for idea in first.ideas)
    assert all(len(idea.visual_prompt) <= 1200 for idea in first.ideas)


def test_plan_rejects_wrong_count_before_any_effect() -> None:
    payload = _valid_payload()
    payload["ideas"] = payload["ideas"][:-1]  # type: ignore[index]

    with pytest.raises(ValidationError):
        MonthlyPlan.model_validate(payload)


def test_plan_rejects_noncanonical_order_and_month() -> None:
    payload = _valid_payload()
    ideas = payload["ideas"]
    assert isinstance(ideas, list)
    ideas[0]["ordinal"] = 2
    with pytest.raises(ValidationError, match="ordinals 1 through 12"):
        validate_monthly_plan(payload)

    payload = _valid_payload()
    ideas = payload["ideas"]
    assert isinstance(ideas, list)
    ideas[0]["month"] = "Jan"
    with pytest.raises(ValidationError, match="canonical January through December"):
        validate_monthly_plan(payload)


def test_plan_rejects_extra_or_out_of_bound_fields() -> None:
    payload = _valid_payload()
    payload["unexpected"] = "nope"
    with pytest.raises(ValidationError):
        MonthlyPlan.model_validate(payload)

    with pytest.raises(ValidationError):
        MonthIdea(
            month="January",
            ordinal=1,
            label="x" * 49,
            visual_prompt="valid",
            spoken_text="valid",
        )
    with pytest.raises(ValidationError):
        MonthIdea(
            month="January",
            ordinal=1,
            label="valid",
            visual_prompt="x" * 1201,
            spoken_text="x" * 81,
        )


def test_plan_rejects_unknown_or_random_persisted_item() -> None:
    payload = _valid_payload()
    payload["item_id"] = "free_text_prompt"
    with pytest.raises(ValidationError, match="item identity"):
        MonthlyPlan.model_validate(payload)

    payload["item_id"] = RANDOM_ITEM_ID
    with pytest.raises(ValidationError, match="item identity"):
        MonthlyPlan.model_validate(payload)


def test_catalog_selection_resolves_random_to_a_concrete_safe_item() -> None:
    chosen = resolve_catalog_item(RANDOM_ITEM_ID, rng=Random(3))

    assert chosen.item_id in MONTHLY_CATALOG
    assert chosen.item_id != RANDOM_ITEM_ID
    assert resolve_catalog_item("chair").label == "Chair"
    with pytest.raises(ValueError, match="approved monthly catalog"):
        resolve_catalog_item("https://untrusted.example/clip.mp4")


def test_serialization_digest_and_display_are_stable() -> None:
    plan = deterministic_fixture_plan(RUN_ID, "cake", revision=2)
    serialized = serialize_monthly_plan(plan)

    assert deserialize_monthly_plan(serialized) == plan
    assert monthly_plan_digest(plan) == plan.canonical_sha256
    assert monthly_plan_digest(plan) == monthly_plan_digest(deserialize_monthly_plan(serialized))

    display = display_monthly_plan_markdown(plan)
    assert "revision 2" in display
    assert "**January**" in display
    assert "**December**" in display


def test_custom_items_and_suggestions_use_the_same_plan_contract() -> None:
    plan = deterministic_fixture_plan(RUN_ID, "  jellyfish lantern  ")
    assert plan.item_text == "jellyfish lantern"
    assert plan.item_id.startswith("custom_")
    assert plan.item_id == resolve_item("jellyfish lantern").item_id
    assert plan.ideas[0].label == "Frozen jellyfish lantern"
    assert plan.canonical_sha256 != deterministic_fixture_plan(RUN_ID, "chair").canonical_sha256
    assert resolve_item("Swimming pool").item_id == "swimming_pool"
    assert resolve_item("swimming_pool").item_id == "swimming_pool"


def test_fixture_display_labels_preserve_a_maximum_length_custom_item() -> None:
    item = "x" * 48

    plan = deterministic_fixture_plan(RUN_ID, item)

    assert plan.item_text == item
    assert plan.item_id == resolve_item(item).item_id
    assert all(len(idea.label) <= 48 for idea in plan.ideas)
    assert all(idea.spoken_text == f"{idea.month}. {idea.label}." for idea in plan.ideas)
    assert all(item in idea.visual_prompt for idea in plan.ideas)


@pytest.mark.parametrize(
    "value", ["", "   ", "x" * 49, "one\ntwo", "a\x00b", "https://example.com/x"]
)
def test_invalid_item_input_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        resolve_item(value)
