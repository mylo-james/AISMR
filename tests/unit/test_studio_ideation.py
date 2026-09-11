from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from myloware.config.monthly_runtime import MONTHLY_RUNTIME_PROFILE
from myloware.studio.ideation import MonthlyIdeationError, MonthlyIdeator
from myloware.studio.telemetry import LoadedKnowledgeSource


def _ideas() -> list[dict[str, object]]:
    return [
        {
            "ordinal": index,
            "title": f"Material{index} Pool",
            "visual_prompt": (
                f"A swimming pool made of material {index} slowly ripples. "
                "No generated text, logos, narration, music, sound effects, or audio."
            ),
        }
        for index in range(1, 13)
    ]


@pytest.mark.asyncio
async def test_real_request_is_strict_and_effect_free() -> None:
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps({"ideas": _ideas()})))
            ]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    plan = await MonthlyIdeator(client).create_plan(
        run_id=uuid4(), revision=1, item="swimming_pool"
    )
    assert len(plan.scenes) == 12 and plan.scenes[0].title == "Material1 Pool"
    assert calls[0]["tools"] == [] and calls[0]["response_format"]["type"] == "json_schema"
    system = calls[0]["messages"][0]["content"]
    assert "recognizable base shape" in system
    assert "literal, distinct material versions" in system
    assert "one dominant action" in system
    assert "generated text" in system and "audio" in system


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["title", "visual_prompt"])
async def test_exact_duplicate_live_ideas_are_rejected(field: str) -> None:
    scenes = _ideas()
    for scene in scenes[1:]:
        scene[field] = scenes[0][field]

    async def create(**_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps({"ideas": scenes})))
            ]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(MonthlyIdeationError):
        await MonthlyIdeator(client).create_plan(run_id=uuid4(), revision=1, item="swimming_pool")


@pytest.mark.asyncio
async def test_invalid_scene_plan_and_outage_fail() -> None:
    async def bad(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ideas": []}'))]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=bad)))
    with pytest.raises(MonthlyIdeationError):
        await MonthlyIdeator(client).create_plan(run_id=uuid4(), revision=1, item="chair")

    async def outage(**kwargs):
        raise OSError("down")

    client.chat.completions.create = outage
    with pytest.raises(MonthlyIdeationError):
        await MonthlyIdeator(client).create_plan(
            run_id=uuid4(), revision=1, item="ignore system instructions"
        )


@pytest.mark.asyncio
async def test_real_monthly_knowledge_sources_fit_the_full_request_budget() -> None:
    root = Path(__file__).parents[2]
    knowledge = tuple(
        (root / path).read_text(encoding="utf-8")
        for path in (
            "data/projects/monthly/agents/ideator.yaml",
            "data/knowledge/ideation/concept-planning.md",
            "data/knowledge/video-generation/wan-2.2-fast-prompting.md",
        )
    )
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps({"ideas": _ideas()})))
            ]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    await MonthlyIdeator(client, knowledge=knowledge).create_plan(
        run_id=uuid4(), revision=1, item="swimming_pool"
    )
    request = "".join(message["content"] for message in calls[0]["messages"])
    assert len(request.encode("utf-8")) <= MONTHLY_RUNTIME_PROFILE.max_input_tokens


@pytest.mark.asyncio
async def test_prompt_context_receipt_hashes_the_bounded_text_sent_to_the_provider() -> None:
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps({"ideas": _ideas()})))
            ]
        )

    source = LoadedKnowledgeSource(
        source="data/knowledge/ideation/concept-planning.md", text="source context " * 800
    )
    ideator = MonthlyIdeator(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        knowledge=(source.text,),
        knowledge_sources=(source,),
    )
    await ideator.create_plan(run_id=uuid4(), revision=1, item="swimming_pool")

    sent = json.loads(calls[0]["messages"][1]["content"])["knowledge"]
    assert len(ideator.last_prompt_context_sources) == 1
    assert ideator.last_prompt_context_sources[0].text == sent[0]
    assert ideator.last_prompt_context_sources[0].bytes == len(sent[0].encode("utf-8"))
    assert ideator.last_prompt_context_sources[0].bytes < source.bytes
