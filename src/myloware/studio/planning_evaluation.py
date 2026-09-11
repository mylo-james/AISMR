"""Offline, blinded comparison support for saved monthly planning outputs."""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from myloware.workflows.scenes import StudioPlan, parse_plan

EVALUATION_SCHEMA_VERSION = 1
DEFAULT_VARIANTS = ("current", "improved", "four_role")


@dataclass(frozen=True)
class Measurement:
    latency_seconds: float | None
    cost_usd: str | None
    status: str


@dataclass(frozen=True)
class ParsedRecord:
    variant: str
    object_id: str
    replicate: int
    plan: StudioPlan | None
    plan_error: str | None
    measurement: Measurement


def compare_saved_plans(
    records: Iterable[Mapping[str, Any]],
    *,
    seed: str,
    expected_variants: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a blinded review packet and a separate unblinding/report artifact.

    `records` are saved results only.  This function does not invoke models, infer
    semantic novelty, or replace missing measurements with zero.
    """

    if not isinstance(seed, str) or not seed:
        raise ValueError("seed must be a non-empty string")
    variants = tuple(expected_variants or DEFAULT_VARIANTS)
    if not variants or len(set(variants)) != len(variants):
        raise ValueError("expected variants must be a non-empty unique sequence")
    parsed = tuple(_parse_record(record) for record in records)
    coverage = _coverage(parsed, variants)
    duplicate_groups = _exact_duplicates(parsed)
    review_packet, key = _blind(parsed, seed=seed)
    report = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "record_count": len(parsed),
        "valid_plan_count": sum(record.plan is not None for record in parsed),
        "invalid_plans": [
            {
                "variant": record.variant,
                "object_id": record.object_id,
                "replicate": record.replicate,
                "reason": record.plan_error,
            }
            for record in parsed
            if record.plan is None
        ],
        "comparability": coverage,
        "exact_duplicate_plan_groups": duplicate_groups,
        "measurements_by_variant": _measurement_summary(parsed),
        "limitations": [
            "Creative-content digests compare normalized object and ideas, excluding run identity and revision.",
            "Full plan identities remain distinct when run IDs or revisions differ.",
            "This artifact does not score semantic novelty, originality, or visual feasibility.",
            "Missing or invalid latency/cost measurements are reported as unknown, never zero.",
        ],
    }
    return {
        "review_packet": {
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "seed_commitment": sha256(seed.encode()).hexdigest(),
            "sets": review_packet,
            "instructions": "Review full plan sets without attempting to identify their source variant.",
        },
        "blinding_key": {"schema_version": EVALUATION_SCHEMA_VERSION, "assignments": key},
        "report": report,
    }


def _parse_record(raw: Mapping[str, Any]) -> ParsedRecord:
    variant = _text(raw.get("variant"), "variant")
    object_id = _text(raw.get("object_id"), "object_id")
    replicate = raw.get("replicate")
    if not isinstance(replicate, int) or isinstance(replicate, bool) or replicate < 1:
        raise ValueError("replicate must be a positive integer")
    measurement = _measurement(raw.get("measurement"))
    try:
        plan = parse_plan(raw.get("plan"))
    except (ValidationError, ValueError, TypeError):
        return ParsedRecord(variant, object_id, replicate, None, "invalid_saved_plan", measurement)
    if plan.item_id != object_id and plan.item_text.casefold() != object_id.casefold():
        return ParsedRecord(
            variant, object_id, replicate, None, "object_identifier_mismatch", measurement
        )
    return ParsedRecord(variant, object_id, replicate, plan, None, measurement)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 96:
        raise ValueError(f"{name} must be a non-empty string up to 96 characters")
    return value.strip()


def _measurement(raw: Any) -> Measurement:
    if not isinstance(raw, Mapping):
        return Measurement(None, None, "unknown")
    latency = raw.get("latency_seconds")
    parsed_latency = (
        float(latency)
        if isinstance(latency, (int, float))
        and not isinstance(latency, bool)
        and math.isfinite(float(latency))
        and latency >= 0
        else None
    )
    cost = raw.get("cost_usd")
    try:
        parsed_cost = Decimal(str(cost)) if cost is not None else None
    except (InvalidOperation, ValueError):
        parsed_cost = None
    if parsed_cost is not None and (not parsed_cost.is_finite() or parsed_cost < 0):
        parsed_cost = None
    if parsed_latency is None and parsed_cost is None:
        return Measurement(None, None, "unknown")
    return Measurement(
        parsed_latency, str(parsed_cost) if parsed_cost is not None else None, "measured"
    )


def _coverage(records: tuple[ParsedRecord, ...], variants: tuple[str, ...]) -> dict[str, Any]:
    expected = set(variants)
    grouped: dict[tuple[str, int], list[ParsedRecord]] = defaultdict(list)
    invalid_candidates: list[dict[str, Any]] = []
    for record in records:
        if record.plan is None:
            invalid_candidates.append(
                {
                    "variant": record.variant,
                    "object_id": record.object_id,
                    "replicate": record.replicate,
                }
            )
            continue
        grouped[(record.object_id, record.replicate)].append(record)
    missing: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []
    duplicate: list[dict[str, Any]] = []
    for (object_id, replicate), entries in sorted(grouped.items()):
        present = Counter(entry.variant for entry in entries)
        for variant in sorted(expected - set(present)):
            missing.append({"object_id": object_id, "replicate": replicate, "variant": variant})
        for variant in sorted(set(present) - expected):
            unexpected.append({"object_id": object_id, "replicate": replicate, "variant": variant})
        for variant, count in sorted(present.items()):
            if count > 1:
                duplicate.append(
                    {
                        "object_id": object_id,
                        "replicate": replicate,
                        "variant": variant,
                        "count": count,
                    }
                )
    return {
        "expected_variants": list(variants),
        "object_replicate_count": len(grouped),
        "comparable": bool(grouped)
        and not missing
        and not unexpected
        and not duplicate
        and not invalid_candidates,
        "missing": missing,
        "unexpected": unexpected,
        "duplicate_records": duplicate,
        "invalid_candidates": invalid_candidates,
    }


def _exact_duplicates(records: tuple[ParsedRecord, ...]) -> list[dict[str, Any]]:
    grouped: dict[str, list[ParsedRecord]] = defaultdict(list)
    for record in records:
        if record.plan is not None:
            grouped[_creative_content_sha256(record.plan)].append(record)
    return [
        {
            "creative_content_sha256": digest,
            "records": [
                {"variant": item.variant, "object_id": item.object_id, "replicate": item.replicate}
                for item in items
            ],
        }
        for digest, items in sorted(grouped.items())
        if len(items) > 1
    ]


def _blind(
    records: tuple[ParsedRecord, ...], *, seed: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[tuple[str, int], list[ParsedRecord]] = defaultdict(list)
    for record in records:
        if record.plan is not None:
            groups[(record.object_id, record.replicate)].append(record)
    packet: list[dict[str, Any]] = []
    key: list[dict[str, Any]] = []
    for object_replicate, entries in sorted(groups.items()):
        ordered = sorted(entries, key=lambda entry: entry.variant)
        # Repeatable review order, not a security token or access decision.
        review_seed = f"{seed}:{object_replicate[0]}:{object_replicate[1]}"
        review_order = random.Random(review_seed)  # nosec B311
        review_order.shuffle(ordered)
        candidates = []
        for index, entry in enumerate(ordered, start=1):
            blind_id = f"set-{index}"
            candidates.append(
                {
                    "blind_id": blind_id,
                    "plan": {
                        "item_id": entry.plan.item_id,
                        "item_text": entry.plan.item_text,
                        "scenes": [idea.model_dump(mode="json") for idea in entry.plan.ideas],
                    },
                }
            )
            key.append(
                {
                    "object_id": object_replicate[0],
                    "replicate": object_replicate[1],
                    "blind_id": blind_id,
                    "variant": entry.variant,
                }
            )
        packet.append(
            {
                "object_id": object_replicate[0],
                "replicate": object_replicate[1],
                "candidates": candidates,
            }
        )
    return packet, key


def _creative_content_sha256(plan: StudioPlan) -> str:
    content = {
        "item_id": plan.item_id,
        "item_text": plan.item_text,
        "scenes": [idea.model_dump(mode="json") for idea in plan.ideas],
    }
    return sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _measurement_summary(records: tuple[ParsedRecord, ...]) -> dict[str, Any]:
    grouped: dict[str, list[Measurement]] = defaultdict(list)
    for record in records:
        grouped[record.variant].append(record.measurement)
    result: dict[str, Any] = {}
    for variant, values in sorted(grouped.items()):
        latencies = [value.latency_seconds for value in values if value.latency_seconds is not None]
        costs = [Decimal(value.cost_usd) for value in values if value.cost_usd is not None]
        result[variant] = {
            "latency_seconds": {
                "measured_count": len(latencies),
                "unknown_count": len(values) - len(latencies),
                "mean": sum(latencies) / len(latencies) if latencies else None,
            },
            "cost_usd": {
                "measured_count": len(costs),
                "unknown_count": len(values) - len(costs),
                "sum": str(sum(costs)) if costs else None,
            },
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an offline blinded monthly-plan comparison")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(source, Mapping) or not isinstance(source.get("records"), list):
        raise TypeError("input JSON must contain a records array")
    artifact = compare_saved_plans(
        source["records"],
        seed=_text(source.get("seed"), "seed"),
        expected_variants=source.get("expected_variants"),
    )
    args.output.write_text(json.dumps(artifact["review_packet"], indent=2) + "\n", encoding="utf-8")
    key_path = args.output.with_name(args.output.stem + ".key.json")
    key_path.write_text(
        json.dumps(
            {"blinding_key": artifact["blinding_key"], "report": artifact["report"]}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
