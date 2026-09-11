from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

import pytest

from myloware.config.monthly_runtime import MONTHLY_IDEATION_RESPONSE_SCHEMA
from myloware.studio.creative_planning import (
    PLANNING_ROLES,
    ConceptCard,
    CreativePlanner,
    CreativePlanningContext,
    CreativePlanningError,
    RecentConcept,
    RetainedIdea,
    RevisionFeedback,
    _PlanningRuntime,
    _Shot,
    _Storyboard,
)
from myloware.workflows.monthly import MonthIdea


def _cards(prefix: str) -> list[dict[str, str]]:
    return [
        {
            "candidate_id": f"{prefix}_{index}",
            "material": f"material {prefix} {index}",
            "action": f"action {prefix} {index}",
            "setting": f"setting {index}",
            "hook": f"hook {index}",
            "payoff": f"payoff {index}",
            "object_fit": "The object keeps its recognizable shape.",
        }
        for index in range(1, 16)
    ]


class _Callback:
    def __init__(self, *, short_first_curation: bool = False, delay_seconds: float = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.short_first_curation = short_first_curation
        self.delay_seconds = delay_seconds

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
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        self.calls.append(
            {
                "role": operation_name,
                "payload": dict(input_payload),
                "schema": response_schema,
                "model": model,
                "prompt": dict(prompt),
                "limits": dict(limits),
                "metadata": dict(metadata),
            }
        )
        assert len((prompt["system"] + prompt["user"]).encode()) <= limits["max_input_bytes"]
        if operation_name == "explore_object":
            return {"cards": _cards("object")}
        if operation_name == "explore_surreal":
            return {"cards": _cards("surreal")}
        if operation_name == "replenish":
            return {"cards": _cards("replenish")}
        count = len(input_payload["replacement_ordinals"])
        if operation_name == "curate" and self.short_first_curation:
            count -= 1
        if operation_name in {"curate", "recurate"}:
            cards = input_payload["cards"]
            return {
                "ranked": [
                    {
                        "candidate_id": cards[index]["candidate_id"],
                        "reason": "varied",
                    }
                    for index in range(count)
                ]
            }
        if operation_name == "write_shots":
            return {
                "shots": [
                    {
                        "ordinal": card["ordinal"],
                        "candidate_id": card["candidate_id"],
                        "title_modifier": f"Material{card['ordinal']}",
                        "title_noun": (
                            input_payload["item"]
                            if len(input_payload["item"]) <= 40
                            else input_payload["item"].split()[-1]
                        ),
                        "storyboard": {
                            "opening_frame": "A recognizable teacup rests centered on a plinth.",
                            "action": f"The literal material performs action {card['ordinal']} visibly.",
                            "payoff_frame": "The teacup settles into a crisp final silhouette.",
                            "camera": "Locked close vertical framing.",
                            "light_and_texture": "Soft side light reveals tactile surface detail.",
                        },
                    }
                    for card in input_payload["selected"]
                ]
            }
        raise AssertionError(operation_name)


@pytest.mark.asyncio
async def test_four_role_plan_is_bounded_and_receipted() -> None:
    callback = _Callback()
    planner = CreativePlanner(object(), model="test-model")

    plan = await planner.create_plan(
        run_id=uuid4(),
        revision=1,
        item="teacup",
        context=CreativePlanningContext(),
        run_operation=callback,
    )

    assert len(plan.ideas) == 12
    assert [call["role"] for call in callback.calls] == [
        "explore_object",
        "explore_surreal",
        "curate",
        "write_shots",
    ]
    assert set(PLANNING_ROLES) == {
        "explore_object",
        "explore_surreal",
        "curate",
        "write_shots",
        "replenish",
        "recurate",
    }
    assert all(call["metadata"]["profile_version"] == "creative-v2" for call in callback.calls)
    assert all(
        call["metadata"]["prompt_version"] == "creative-planning-v9" for call in callback.calls
    )
    assert all(call["metadata"]["schema_version"] == 5 for call in callback.calls)
    assert all(call["limits"]["deadline_seconds"] == 120 for call in callback.calls)
    assert plan.scenes[0].title == "Material1 Teacup"
    writer_schema = next(call for call in callback.calls if call["role"] == "write_shots")
    schema = writer_schema["schema"]["schema"]
    shot_reference = schema["properties"]["shots"]["items"]["$ref"]
    writer_fields = schema["$defs"][shot_reference.rsplit("/", maxsplit=1)[-1]]["properties"]
    assert "material" not in writer_fields
    assert "action" not in writer_fields
    writer_ids = [entry["candidate_id"] for entry in writer_schema["payload"]["selected"]]
    assert writer_fields["candidate_id"]["enum"] == writer_ids
    assert writer_fields["title_noun"]["enum"] == ["Teacup"]
    assert all(scene.title.endswith("Teacup") for scene in plan.scenes)


@pytest.mark.asyncio
async def test_writer_title_noun_schema_matches_scene_plan_for_multiword_custom_item() -> None:
    callback = _Callback()
    plan = await CreativePlanner(object(), model="test-model").create_plan(
        run_id=uuid4(),
        revision=1,
        item="tiny antique teacup",
        context=CreativePlanningContext(),
        run_operation=callback,
    )
    writer_call = next(call for call in callback.calls if call["role"] == "write_shots")
    schema = writer_call["schema"]["schema"]
    shot_reference = schema["properties"]["shots"]["items"]["$ref"]
    writer_fields = schema["$defs"][shot_reference.rsplit("/", maxsplit=1)[-1]]["properties"]
    assert writer_fields["title_noun"]["enum"] == ["tiny antique teacup"]
    assert all(scene.title.endswith("tiny antique teacup") for scene in plan.scenes)
    assert plan.item_text == "tiny antique teacup"


@pytest.mark.asyncio
async def test_writer_uses_valid_custom_noun_phrase_enum_when_full_label_exceeds_field_limit() -> (
    None
):
    item = "weathered old hand-thrown porcelain teacup"
    callback = _Callback()
    plan = await CreativePlanner(object(), model="test-model").create_plan(
        run_id=uuid4(),
        revision=1,
        item=item,
        context=CreativePlanningContext(),
        run_operation=callback,
    )
    writer_call = next(call for call in callback.calls if call["role"] == "write_shots")
    schema = writer_call["schema"]["schema"]
    shot_reference = schema["properties"]["shots"]["items"]["$ref"]
    title_nouns = schema["$defs"][shot_reference.rsplit("/", maxsplit=1)[-1]]["properties"][
        "title_noun"
    ]["enum"]
    assert item not in title_nouns
    assert title_nouns
    assert all(len(noun) <= 40 and noun in item for noun in title_nouns)
    assert all(scene.title.endswith("teacup") for scene in plan.scenes)
    assert plan.item_text == item


@pytest.mark.asyncio
async def test_curator_schema_enumerates_exact_offered_ids_and_forbids_positional_substitutes() -> (
    None
):
    offered_ids = ("object_03", "object_06", "object_14")
    cards = tuple(
        ConceptCard.model_validate({**_cards("object")[index], "candidate_id": candidate_id})
        for index, candidate_id in enumerate(offered_ids)
    )
    callback = _Callback()
    runtime = _PlanningRuntime(
        planner=CreativePlanner(object(), model="test-model"),
        run_id=uuid4(),
        revision=1,
        item="teacup",
        context=CreativePlanningContext(),
        replacement_ordinals=(1, 2, 3),
        run_operation=callback,
    )
    await runtime.select("curate", cards, allow_short=True)
    call = callback.calls[-1]
    ranked = call["schema"]["schema"]["properties"]["ranked"]
    choice_ref = ranked["items"]["$ref"].rsplit("/", maxsplit=1)[-1]
    candidate_schema = call["schema"]["schema"]["$defs"][choice_ref]["properties"]["candidate_id"]
    assert candidate_schema["enum"] == list(offered_ids)
    assert "candidate_03" not in candidate_schema["enum"]
    assert "Copy each candidate_id exactly from the offered cards" in call["prompt"]["system"]


@pytest.mark.asyncio
async def test_one_replenishment_cycle_is_the_six_call_limit() -> None:
    callback = _Callback(short_first_curation=True)
    plan = await CreativePlanner(object(), model="test-model").create_plan(
        run_id=uuid4(),
        revision=1,
        item="teacup",
        context=CreativePlanningContext(),
        run_operation=callback,
    )

    assert len(plan.ideas) == 12
    assert [call["role"] for call in callback.calls] == [
        "explore_object",
        "explore_surreal",
        "curate",
        "replenish",
        "recurate",
        "write_shots",
    ]
    final_selection = next(call for call in callback.calls if call["role"] == "recurate")
    ranked_schema = final_selection["schema"]["schema"]["properties"]["ranked"]
    assert ranked_schema["minItems"] == ranked_schema["maxItems"] == 12
    choice_ref = ranked_schema["items"]["$ref"].rsplit("/", maxsplit=1)[-1]
    recurate_candidate_schema = final_selection["schema"]["schema"]["$defs"][choice_ref][
        "properties"
    ]["candidate_id"]
    assert recurate_candidate_schema["enum"] == [
        card["candidate_id"] for card in final_selection["payload"]["cards"]
    ]
    assert "Return exactly 12" in final_selection["prompt"]["system"]
    assert "You may return fewer" not in final_selection["prompt"]["system"]


@pytest.mark.asyncio
async def test_workflow_deadline_can_cover_more_than_one_per_call_window() -> None:
    callback = _Callback(short_first_curation=True, delay_seconds=0.22)
    loop = asyncio.get_running_loop()
    started = loop.time()
    plan = await CreativePlanner(
        object(), model="test-model", deadline_seconds=1, workflow_deadline_seconds=2
    ).create_plan(
        run_id=uuid4(),
        revision=1,
        item="teacup",
        context=CreativePlanningContext(),
        run_operation=callback,
    )

    assert len(plan.ideas) == 12
    assert loop.time() - started > 1
    assert all(call["limits"]["deadline_seconds"] == 1 for call in callback.calls)


@pytest.mark.asyncio
async def test_targeted_revision_preserves_retained_idea_exactly() -> None:
    retained = MonthIdea(
        month="January",
        ordinal=1,
        label="Retained Teacup",
        visual_prompt="The exact retained prompt.",
        spoken_text="January. Retained Teacup.",
    )
    context = CreativePlanningContext(
        retained_ideas=(RetainedIdea(ordinal=1, idea=retained),), retained_plan_hash="a" * 64
    )

    plan = await CreativePlanner(object(), model="test-model").create_plan(
        run_id=uuid4(), revision=2, item="teacup", context=context, run_operation=_Callback()
    )

    assert plan.scenes[0].title == retained.label
    assert plan.ideas[1].ordinal == 2


def test_provider_schema_leaves_backend_narration_out_of_the_response() -> None:
    idea = MONTHLY_IDEATION_RESPONSE_SCHEMA["schema"]["properties"]["ideas"]["items"]
    assert "spoken_text" not in idea["required"]
    assert "spoken_text" not in idea["properties"]


@pytest.mark.asyncio
async def test_malformed_selection_fails_closed() -> None:
    class UnknownSelection(_Callback):
        async def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
            if kwargs["operation_name"] == "curate":
                await super().__call__(**kwargs)
                return {"ranked": [{"candidate_id": "missing_1", "reason": "bad"}]}
            return await super().__call__(**kwargs)

    with pytest.raises(CreativePlanningError, match="unknown card"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(),
            revision=1,
            item="teacup",
            context=CreativePlanningContext(),
            run_operation=UnknownSelection(),
        )


@pytest.mark.asyncio
async def test_curator_rank_order_maps_deterministically_to_partial_revision_ordinals() -> None:
    class RankedCallback(_Callback):
        async def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
            if kwargs["operation_name"] == "curate":
                cards = kwargs["input_payload"]["cards"]
                return {
                    "ranked": [
                        {"candidate_id": cards[4]["candidate_id"], "reason": "first"},
                        {"candidate_id": cards[1]["candidate_id"], "reason": "second"},
                        {"candidate_id": cards[7]["candidate_id"], "reason": "third"},
                    ]
                }
            return await super().__call__(**kwargs)

    runtime = _PlanningRuntime(
        planner=CreativePlanner(object(), model="test-model"),
        run_id=uuid4(),
        revision=2,
        item="teacup",
        context=CreativePlanningContext(),
        replacement_ordinals=(2, 3, 9),
        run_operation=RankedCallback(),
    )
    selected = await runtime.select(
        "curate",
        tuple(ConceptCard.model_validate(card) for card in _cards("object")),
        allow_short=True,
    )
    assert [(entry.ordinal, entry.candidate_id) for entry in selected] == [
        (2, "object_5"),
        (3, "object_2"),
        (9, "object_8"),
    ]


@pytest.mark.asyncio
async def test_curator_repeated_ranked_candidate_fails_closed() -> None:
    class DuplicateRanked(_Callback):
        async def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
            if kwargs["operation_name"] == "curate":
                card_id = kwargs["input_payload"]["cards"][0]["candidate_id"]
                return {
                    "ranked": [
                        {"candidate_id": card_id, "reason": "one"},
                        {"candidate_id": card_id, "reason": "two"},
                    ]
                }
            return await super().__call__(**kwargs)

    with pytest.raises(CreativePlanningError, match="repeated a selection"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(),
            revision=1,
            item="teacup",
            context=CreativePlanningContext(),
            run_operation=DuplicateRanked(),
        )


@pytest.mark.asyncio
async def test_writer_response_cannot_supply_selected_facts() -> None:
    class DriftWriter(_Callback):
        async def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
            result = await super().__call__(**kwargs)
            if kwargs["operation_name"] == "write_shots":
                result["shots"][0]["material"] = "different material"
            return result

    with pytest.raises(CreativePlanningError, match="invalid result"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(),
            revision=1,
            item="teacup",
            context=CreativePlanningContext(),
            run_operation=DriftWriter(),
        )


@pytest.mark.asyncio
async def test_writer_binds_selected_material_and_action_internally() -> None:
    callback = _Callback()
    runtime = _PlanningRuntime(
        planner=CreativePlanner(object(), model="test-model"),
        run_id=uuid4(),
        revision=1,
        item="teacup",
        context=CreativePlanningContext(),
        replacement_ordinals=tuple(range(1, 13)),
        run_operation=callback,
    )
    state = await runtime.graph().ainvoke({})
    selected = {entry.ordinal: entry.candidate_id for entry in state["selected"]}
    cards = {card.candidate_id: card for card in state["candidates"]}
    assert all(
        shot.material == cards[selected[shot.ordinal]].material
        and shot.action == cards[selected[shot.ordinal]].action
        for shot in state["shots"]
    )
    assert "Recognizable teacup with literal" in runtime.to_plan(state).scenes[0].visual_prompt


@pytest.mark.asyncio
async def test_writer_wrong_candidate_and_duplicate_titles_fail_closed() -> None:
    class WrongCandidate(_Callback):
        async def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
            result = await super().__call__(**kwargs)
            if kwargs["operation_name"] == "write_shots":
                result["shots"][0]["candidate_id"] = "surreal_1"
            return result

    with pytest.raises(CreativePlanningError, match="changed selected candidate identity"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(),
            revision=1,
            item="teacup",
            context=CreativePlanningContext(),
            run_operation=WrongCandidate(),
        )

    class DuplicateTitles(_Callback):
        async def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
            result = await super().__call__(**kwargs)
            if kwargs["operation_name"] == "write_shots":
                for shot in result["shots"]:
                    shot["title_modifier"] = "Frozen"
            return result

    with pytest.raises(CreativePlanningError, match="writer titles must be unique"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(),
            revision=1,
            item="teacup",
            context=CreativePlanningContext(),
            run_operation=DuplicateTitles(),
        )

    class WrongObjectNoun(_Callback):
        async def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
            result = await super().__call__(**kwargs)
            if kwargs["operation_name"] == "write_shots":
                result["shots"][0]["title_noun"] = "Teapot"
            return result

    with pytest.raises(CreativePlanningError, match="selected object noun"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(),
            revision=1,
            item="teacup",
            context=CreativePlanningContext(),
            run_operation=WrongObjectNoun(),
        )


@pytest.mark.asyncio
async def test_writer_omission_fails_closed() -> None:
    class OmitWriter(_Callback):
        async def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
            result = await super().__call__(**kwargs)
            if kwargs["operation_name"] == "write_shots":
                result["shots"] = result["shots"][:-1]
            return result

    with pytest.raises(CreativePlanningError, match="exactly one shot"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(),
            revision=1,
            item="teacup",
            context=CreativePlanningContext(),
            run_operation=OmitWriter(),
        )


@pytest.mark.asyncio
async def test_retained_exact_duplicate_label_is_rejected() -> None:
    retained = MonthIdea(
        month="January",
        ordinal=1,
        label="Material2 Teacup",
        visual_prompt="Retained prompt.",
        spoken_text="January. Material2 Teacup.",
    )
    context = CreativePlanningContext(
        retained_ideas=(RetainedIdea(ordinal=1, idea=retained),), retained_plan_hash="b" * 64
    )
    with pytest.raises(CreativePlanningError, match="writer titles must be unique"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(), revision=2, item="teacup", context=context, run_operation=_Callback()
        )


@pytest.mark.asyncio
async def test_replenishment_receives_existing_cards_and_rejects_id_collision() -> None:
    class CollidingReplenishment(_Callback):
        async def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
            role = kwargs["operation_name"]
            if role == "replenish":
                assert kwargs["input_payload"]["existing_cards"]
                assert kwargs["input_payload"]["selected_ids"]
                assert kwargs["input_payload"]["missing_ordinals"] == [12]
                await super().__call__(**kwargs)
                return {"cards": _cards("object")}
            return await super().__call__(**kwargs)

    with pytest.raises(CreativePlanningError, match="outside its namespace"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(),
            revision=1,
            item="teacup",
            context=CreativePlanningContext(),
            run_operation=CollidingReplenishment(short_first_curation=True),
        )


@pytest.mark.asyncio
async def test_retained_exact_duplicate_prompt_is_rejected() -> None:
    duplicate_prompt = _PlanningRuntime.compile_visual_prompt(
        _Shot(
            ordinal=2,
            candidate_id="object_1",
            object_name="teacup",
            material="material object 1",
            action="action object 1",
            title_modifier="Material",
            title_noun="Teacup",
            storyboard=_Storyboard(
                opening_frame="A recognizable teacup rests centered on a plinth.",
                action="The literal material performs action 2 visibly.",
                payoff_frame="The teacup settles into a crisp final silhouette.",
                camera="Locked close vertical framing.",
                light_and_texture="Soft side light reveals tactile surface detail.",
            ),
        )
    )
    retained = MonthIdea(
        month="January",
        ordinal=1,
        label="Retained Teacup",
        visual_prompt=duplicate_prompt,
        spoken_text="January. Retained Teacup.",
    )
    context = CreativePlanningContext(
        retained_ideas=(RetainedIdea(ordinal=1, idea=retained),), retained_plan_hash="c" * 64
    )
    with pytest.raises(CreativePlanningError, match="repeated a visual prompt"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(), revision=2, item="teacup", context=context, run_operation=_Callback()
        )


@pytest.mark.asyncio
async def test_curator_payload_bound_covers_45_maximum_size_cards() -> None:
    retained = tuple(
        RetainedIdea(
            ordinal=index,
            idea=MonthIdea(
                month=(
                    "January",
                    "February",
                    "March",
                    "April",
                    "May",
                    "June",
                    "July",
                    "August",
                    "September",
                    "October",
                    "November",
                )[index - 1],
                ordinal=index,
                label="L" * 48,
                visual_prompt="P" * 1200,
                spoken_text="S" * 80,
            ),
        )
        for index in range(1, 12)
    )
    context = CreativePlanningContext(
        recent_concepts=tuple(
            RecentConcept(material="m" * 80, action="a" * 120, setting="s" * 160, outcome="unknown")
            for _ in range(12)
        ),
        revision_feedback=tuple(
            RevisionFeedback(category="more_variety", note="n" * 500) for _ in range(6)
        ),
        retained_ideas=retained,
        retained_plan_hash="d" * 64,
    )
    cards = tuple(
        ConceptCard(
            candidate_id=f"replenish_{index}",
            material="m" * 40,
            action="a" * 120,
            setting="s" * 80,
            hook="h" * 120,
            payoff="p" * 120,
            object_fit="o" * 80,
        )
        for index in range(1, 46)
    )
    callback = _Callback()
    runtime = _PlanningRuntime(
        planner=CreativePlanner(object(), model="test-model"),
        run_id=uuid4(),
        revision=2,
        item="teacup",
        context=context,
        replacement_ordinals=(12,),
        run_operation=callback,
    )
    await runtime.select("recurate", cards, allow_short=False)
    assert callback.calls[-1]["role"] == "recurate"


@pytest.mark.asyncio
async def test_writer_requires_complete_nonblank_storyboard_and_compiles_it() -> None:
    class MissingStoryboard(_Callback):
        async def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
            result = await super().__call__(**kwargs)
            if kwargs["operation_name"] == "write_shots":
                result["shots"][0]["storyboard"].pop("payoff_frame")
            return result

    with pytest.raises(CreativePlanningError, match="invalid result"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(),
            revision=1,
            item="teacup",
            context=CreativePlanningContext(),
            run_operation=MissingStoryboard(),
        )

    plan = await CreativePlanner(object(), model="test-model").create_plan(
        run_id=uuid4(),
        revision=1,
        item="teacup",
        context=CreativePlanningContext(),
        run_operation=_Callback(),
    )
    prompt = plan.ideas[0].visual_prompt
    assert "Opening frame, about 0-1 seconds:" in prompt
    assert (
        "One material-driven action, about 1-4.7 seconds: The literal material performs action 1 visibly."
        in prompt
    )
    assert "Payoff frame, held about 4.7-6.7 seconds:" in prompt
    assert "Camera:" in prompt
    assert "Light and texture:" in prompt
    assert len(prompt) <= 1200


def test_storyboard_compilation_normalizes_terminal_punctuation_and_has_timing_labels() -> None:
    shot = _Shot(
        ordinal=1,
        candidate_id="object_1",
        material="glass",
        action="folds",
        title_modifier="Glass",
        title_noun="Teacup",
        storyboard=_Storyboard(
            opening_frame="The glass teacup waits on a round plinth...",
            action="Its handle bends inward, then slowly uncurls,",
            payoff_frame="The teacup stands whole again!",
            camera="Fixed waist-high close framing;",
            light_and_texture="Cool sidelight catches translucent ridges.",
        ),
    )
    prompt = _PlanningRuntime.compile_visual_prompt(shot)
    assert "plinth...." not in prompt
    assert "uncurls,." not in prompt
    assert "0-1 seconds" in prompt
    assert "1-4.7 seconds" in prompt
    assert "4.7-6.7 seconds" in prompt
    assert len(prompt) <= 1200


@pytest.mark.asyncio
async def test_writer_rejects_blank_storyboard_field_and_candidate_drift() -> None:
    class InvalidBoard(_Callback):
        async def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
            result = await super().__call__(**kwargs)
            if kwargs["operation_name"] == "write_shots":
                result["shots"][0]["storyboard"]["camera"] = "   "
            return result

    with pytest.raises(CreativePlanningError, match="invalid result"):
        await CreativePlanner(object(), model="test-model").create_plan(
            run_id=uuid4(),
            revision=1,
            item="teacup",
            context=CreativePlanningContext(),
            run_operation=InvalidBoard(),
        )
