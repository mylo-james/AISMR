"""Validated, immutable contracts for the AISMR monthly package.

This module deliberately has no provider, storage, worker, or approval side
effects.  Those owners consume a validated :class:`MonthlyPlan` later in the
workflow.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from hashlib import sha256
from random import Random, SystemRandom
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

CANONICAL_MONTHS: tuple[str, ...] = (
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
    "December",
)
"""The only calendar sequence accepted by a monthly plan."""

RANDOM_ITEM_ID = "random"
"""The public selection token. Stored plans always contain a resolved item identity."""

FIXTURE_MATERIALS: tuple[str, ...] = (
    "Frozen",
    "Crystal",
    "Velvet",
    "Moss",
    "Porcelain",
    "Amber",
    "Paper",
    "Cloud",
    "Obsidian",
    "Candlewax",
    "Moonstone",
    "Glass",
)
"""Stable material variants used only by the provider-free fixture plan."""


class MonthlyCatalogItem(BaseModel):
    """One suggested or visitor-entered item; moderation is a separate required step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=48)


_CATALOG_ITEMS = (
    MonthlyCatalogItem(item_id="swimming_pool", label="Swimming pool"),
    MonthlyCatalogItem(item_id="chair", label="Chair"),
    MonthlyCatalogItem(item_id="bed", label="Bed"),
    MonthlyCatalogItem(item_id="cake", label="Cake"),
    MonthlyCatalogItem(item_id="teacup", label="Teacup"),
    MonthlyCatalogItem(item_id="lamp", label="Lamp"),
    MonthlyCatalogItem(item_id="snow_globe", label="Snow globe"),
    MonthlyCatalogItem(item_id="watermelon", label="Watermelon"),
    MonthlyCatalogItem(item_id="pumpkin", label="Pumpkin"),
    MonthlyCatalogItem(item_id="rain_boot", label="Rain boot"),
)

MONTHLY_CATALOG: Mapping[str, MonthlyCatalogItem] = MappingProxyType(
    {item.item_id: item for item in _CATALOG_ITEMS}
)
"""Version-one suggestions. Visitors can also type their own item."""


class MonthIdea(BaseModel):
    """One calendar-positioned idea in an approved monthly plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    month: str = Field(min_length=1, max_length=9)
    ordinal: StrictInt = Field(ge=1, le=12)
    label: str = Field(min_length=1, max_length=48)
    visual_prompt: str = Field(min_length=1, max_length=1200)
    spoken_text: str = Field(min_length=1, max_length=80)


def _fixture_display_label(material: str, item_label: str) -> str:
    """Keep fixture labels valid while retaining the full item in plan metadata and prompts."""

    item_limit = 48 - len(material) - 1
    if item_limit < 1:  # pragma: no cover - guarded by the fixed material list above.
        raise ValueError("fixture material leaves no room for an item label")
    return f"{material} {item_label[:item_limit]}"


class MonthlyPlan(BaseModel):
    """An immutable twelve-entry idea plan, before any media effect is started."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: UUID
    revision: StrictInt = Field(ge=1)
    item_id: str = Field(min_length=1, max_length=48)
    item_text: str = Field(min_length=1, max_length=48)
    ideas: tuple[MonthIdea, ...] = Field(min_length=12, max_length=12)

    @field_validator("item_text")
    @classmethod
    def validate_item_text(cls, item_text: str) -> str:
        return normalize_item_text(item_text)

    @model_validator(mode="after")
    def require_canonical_calendar(self) -> MonthlyPlan:
        if self.item_id != resolve_item(self.item_text).item_id:
            raise ValueError("item identity must match the resolved item text")
        for expected_ordinal, (expected_month, idea) in enumerate(
            zip(CANONICAL_MONTHS, self.ideas, strict=True), start=1
        ):
            if idea.ordinal != expected_ordinal:
                raise ValueError("ideas must use ordinals 1 through 12 in calendar order")
            if idea.month != expected_month:
                raise ValueError("ideas must use canonical January through December month names")
        return self

    @property
    def canonical_sha256(self) -> str:
        """Return the digest of the stable JSON representation used for evidence."""

        return monthly_plan_digest(self)


def resolve_catalog_item(selection: str, *, rng: Random | None = None) -> MonthlyCatalogItem:
    """Resolve an explicit catalog ID or choose a catalog item on the server.

    The returned value is always a concrete catalog item.  The ``random`` token
    is therefore never written into a :class:`MonthlyPlan`.
    """

    if not isinstance(selection, str) or not selection:
        raise ValueError("selection must be a non-empty catalog ID or 'random'")
    if selection == RANDOM_ITEM_ID:
        chooser = rng if rng is not None else SystemRandom()
        return chooser.choice(tuple(MONTHLY_CATALOG.values()))
    try:
        return MONTHLY_CATALOG[selection]
    except KeyError as exc:
        raise ValueError("selection must be an approved monthly catalog ID or 'random'") from exc


def normalize_item_text(value: str) -> str:
    """Normalize one item without interpreting it as instructions or a URL."""
    if not isinstance(value, str):
        raise ValueError("Enter an item")  # noqa: TRY004 - Pydantic validators require ValueError
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > 48:
        raise ValueError("Enter an item using 1 to 48 characters")
    if any(unicodedata.category(char).startswith("C") or char in "\r\n" for char in normalized):
        raise ValueError("Enter a single item without control characters")
    if re.search(
        r"(?:https?://|www\.|data:|file:|\S+\.(?:com|net|org)(?:/|$))",
        normalized,
        re.IGNORECASE,
    ):
        raise ValueError("Enter an item, not a URL")
    return " ".join(normalized.split())


def resolve_item(selection: str, *, rng: Random | None = None) -> MonthlyCatalogItem:
    """Resolve suggestions, Random or free text; this does not grant a safety verdict."""
    normalized = normalize_item_text(selection)
    if normalized.lower() == RANDOM_ITEM_ID:
        return resolve_catalog_item(RANDOM_ITEM_ID, rng=rng)
    for item in MONTHLY_CATALOG.values():
        if normalized.casefold() in {item.item_id, item.label.casefold()}:
            return item
    item_id = "custom_" + sha256(normalized.casefold().encode("utf-8")).hexdigest()[:32]
    return MonthlyCatalogItem(item_id=item_id, label=normalized)


def deterministic_fixture_plan(run_id: UUID | str, item_id: str, revision: int = 1) -> MonthlyPlan:
    """Build a stable local fixture without calling an ideation provider.

    Passing ``random`` is deterministic here so fixture evidence remains
    reproducible. Runtime admission should instead use :func:`resolve_item`
    and persist its returned concrete ID.
    """

    parsed_run_id = UUID(str(run_id))
    if item_id == RANDOM_ITEM_ID:
        index = int(sha256(parsed_run_id.bytes).hexdigest(), 16) % len(MONTHLY_CATALOG)
        item = tuple(MONTHLY_CATALOG.values())[index]
    else:
        item = resolve_item(item_id)

    def fixture_idea(ordinal: int, month: str, material: str) -> MonthIdea:
        label = _fixture_display_label(material, item.label)
        return MonthIdea(
            ordinal=ordinal,
            month=month,
            label=label,
            visual_prompt=(
                f"A single {material.lower()} {item.label.lower()} in a calm surreal "
                f"{month.lower()} scene, with one simple visible motion. No people, "
                "readable words, logos, audio, URLs, or uploaded references."
            ),
            spoken_text=f"{month}. {label}.",
        )

    ideas = tuple(
        fixture_idea(ordinal, month, material)
        for ordinal, (month, material) in enumerate(
            zip(CANONICAL_MONTHS, FIXTURE_MATERIALS, strict=True), start=1
        )
    )
    return MonthlyPlan(
        run_id=parsed_run_id,
        revision=revision,
        item_id=item.item_id,
        item_text=item.label,
        ideas=ideas,
    )


def validate_monthly_plan(payload: MonthlyPlan | Mapping[str, Any]) -> MonthlyPlan:
    """Validate an untrusted object before it reaches a provider or persistence owner."""

    if isinstance(payload, MonthlyPlan):
        return payload
    return MonthlyPlan.model_validate(payload)


def serialize_monthly_plan(plan: MonthlyPlan | Mapping[str, Any]) -> str:
    """Serialize a validated plan to stable, canonical JSON."""

    validated = validate_monthly_plan(plan)
    return json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def deserialize_monthly_plan(serialized: str | bytes | bytearray) -> MonthlyPlan:
    """Deserialize and validate canonical or ordinary JSON plan data."""

    return MonthlyPlan.model_validate_json(serialized)


def monthly_plan_digest(plan: MonthlyPlan | Mapping[str, Any]) -> str:
    """Return the SHA-256 digest of a plan's canonical JSON representation."""

    return sha256(serialize_monthly_plan(plan).encode("utf-8")).hexdigest()


def display_monthly_plan_markdown(plan: MonthlyPlan | Mapping[str, Any]) -> str:
    """Render the review-safe visible plan, without granting an approval decision."""

    validated = validate_monthly_plan(plan)
    heading = f"# AISMR monthly plan, revision {validated.revision}"
    entries = [
        f"{idea.ordinal}. **{idea.month}**: {idea.label}. Spoken: {idea.spoken_text}"
        for idea in validated.ideas
    ]
    return "\n".join((heading, "", *entries))


__all__ = [
    "CANONICAL_MONTHS",
    "MONTHLY_CATALOG",
    "RANDOM_ITEM_ID",
    "MonthIdea",
    "MonthlyCatalogItem",
    "MonthlyPlan",
    "deserialize_monthly_plan",
    "deterministic_fixture_plan",
    "display_monthly_plan_markdown",
    "monthly_plan_digest",
    "normalize_item_text",
    "resolve_catalog_item",
    "resolve_item",
    "serialize_monthly_plan",
    "validate_monthly_plan",
]
