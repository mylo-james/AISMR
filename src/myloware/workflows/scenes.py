"""Validated scene-only contracts for AISMR's version-two planning pipeline.

This module has no provider, storage, renderer, or approval effects.  Calendar
assembly belongs to the version-two renderer, never to these creation inputs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from myloware.workflows.monthly import (
    FIXTURE_MATERIALS,
    MONTHLY_CATALOG,
    RANDOM_ITEM_ID,
    MonthlyPlan,
    _fixture_display_label,
    normalize_item_text,
    resolve_item,
)


class SceneIdea(BaseModel):
    """One ordered visual scene without calendar or narration fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: StrictInt = Field(ge=1, le=12)
    title: str = Field(min_length=1, max_length=48)
    visual_prompt: str = Field(min_length=1, max_length=1200)

    @field_validator("title")
    @classmethod
    def require_title_words(cls, value: str) -> str:
        if (
            value != " ".join(value.split())
            or len(value.split()) < 2
            or any(not (character.isalnum() or character in " '-’") for character in value)
        ):
            raise ValueError("title must contain a modifier and object noun")
        return value

    @field_validator("visual_prompt")
    @classmethod
    def require_visible_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("visual_prompt cannot be blank")
        return value

    @property
    def label(self) -> str:
        """Compatibility view for generic readers.  It is never serialized."""

        return self.title


def title_retains_object_noun(*, title: str, item_id: str, item_text: str) -> bool:
    """Shared noun rule for final plans and targeted validation diagnostics."""
    object_words = item_text.casefold().split()
    noun_words = title.casefold().split()[1:]
    if item_id in MONTHLY_CATALOG:
        return noun_words == object_words[-len(noun_words) :]
    return any(
        noun_words == object_words[start : start + len(noun_words)]
        for start in range(len(object_words))
    )


class ScenePlan(BaseModel):
    """An immutable twelve-scene plan before any media effect starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    run_id: UUID
    revision: StrictInt = Field(ge=1)
    item_id: str = Field(min_length=1, max_length=48)
    item_text: str = Field(min_length=1, max_length=48)
    scenes: tuple[SceneIdea, ...] = Field(min_length=12, max_length=12)

    @field_validator("item_text")
    @classmethod
    def validate_item_text(cls, value: str) -> str:
        return normalize_item_text(value)

    @model_validator(mode="after")
    def require_ordered_scenes(self) -> ScenePlan:
        if resolve_item(self.item_text).item_id != self.item_id:
            raise ValueError("item identity must match the resolved item text")
        for expected, scene in enumerate(self.scenes, start=1):
            if scene.ordinal != expected:
                raise ValueError("scenes must use ordered ordinals 1 through 12")
            if not title_retains_object_noun(
                title=scene.title, item_id=self.item_id, item_text=self.item_text
            ):
                raise ValueError("scene title must retain the selected object noun")
        if len({scene.title.casefold() for scene in self.scenes}) != 12:
            raise ValueError("scene titles must be distinct")
        return self

    @property
    def ideas(self) -> tuple[SceneIdea, ...]:
        """Compatibility view for generic consumers.  It is never serialized."""

        return self.scenes

    @property
    def canonical_sha256(self) -> str:
        return scene_plan_digest(self)


type StudioPlan = MonthlyPlan | ScenePlan


def parse_plan(value: StudioPlan | Mapping[str, Any] | str | bytes | bytearray) -> StudioPlan:
    """Strictly dispatch a persisted plan by its explicit schema version."""

    if isinstance(value, (MonthlyPlan, ScenePlan)):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        # Persisted schema errors share the ValueError handling used by callers.
        raise ValueError("plan must be an object with a schema_version")  # noqa: TRY004
    version = value.get("schema_version")
    if type(version) is not int:
        raise ValueError("plan schema_version must be 1 or 2")
    if version == 1:
        return MonthlyPlan.model_validate(value)
    if version == 2:
        return ScenePlan.model_validate(value)
    raise ValueError("plan schema_version must be 1 or 2")


def serialize_scene_plan(plan: ScenePlan | Mapping[str, Any]) -> str:
    validated = plan if isinstance(plan, ScenePlan) else ScenePlan.model_validate(plan)
    return json.dumps(
        validated.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def scene_plan_digest(plan: ScenePlan | Mapping[str, Any]) -> str:
    return sha256(serialize_scene_plan(plan).encode("utf-8")).hexdigest()


def deterministic_scene_plan(run_id: UUID | str, item_id: str, revision: int = 1) -> ScenePlan:
    """Build a provider-free v2 fixture without calendar inputs or narration."""

    parsed_run_id = UUID(str(run_id))
    if item_id == RANDOM_ITEM_ID:
        index = int(sha256(parsed_run_id.bytes).hexdigest(), 16) % 10
        item = tuple(
            resolve_item(name)
            for name in (
                "swimming_pool",
                "chair",
                "bed",
                "cake",
                "teacup",
                "lamp",
                "snow_globe",
                "watermelon",
                "pumpkin",
                "rain_boot",
            )
        )[index]
    else:
        item = resolve_item(item_id)
    scenes = tuple(
        SceneIdea(
            ordinal=ordinal,
            title=_fixture_display_label(
                material, item.label if len(item.label) <= 32 else item.label.split()[0]
            ),
            visual_prompt=(
                f"A single {material.lower()} {item.label.lower()} in a calm surreal scene, with "
                "one simple visible motion. No people, readable words, logos, audio, URLs, or "
                "uploaded references."
            ),
        )
        for ordinal, material in enumerate(FIXTURE_MATERIALS, start=1)
    )
    return ScenePlan(
        run_id=parsed_run_id,
        revision=revision,
        item_id=item.item_id,
        item_text=item.label,
        scenes=scenes,
    )


__all__ = [
    "SceneIdea",
    "ScenePlan",
    "StudioPlan",
    "deterministic_scene_plan",
    "parse_plan",
    "scene_plan_digest",
    "serialize_scene_plan",
]
