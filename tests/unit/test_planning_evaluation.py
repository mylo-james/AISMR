from __future__ import annotations

import json
from uuid import uuid4

from myloware.studio.planning_evaluation import compare_saved_plans, main
from myloware.workflows.monthly import deterministic_fixture_plan


def _record(variant: str, replicate: int, *, measurement: object = None) -> dict:
    plan = deterministic_fixture_plan(uuid4(), "teacup", revision=replicate)
    result = {
        "variant": variant,
        "object_id": "teacup",
        "replicate": replicate,
        "plan": plan.model_dump(mode="json"),
    }
    if measurement is not None:
        result["measurement"] = measurement
    return result


def test_blinding_is_stable_and_keeps_variant_out_of_review_packet() -> None:
    records = [
        _record(variant, 1, measurement={"latency_seconds": 1.2, "cost_usd": "0.02"})
        for variant in ("current", "improved", "four_role")
    ]
    first = compare_saved_plans(records, seed="stable")
    second = compare_saved_plans(records, seed="stable")

    assert first["review_packet"] == second["review_packet"]
    assert first["report"]["comparability"]["comparable"] is True
    assert "current" not in json.dumps(first["review_packet"])
    candidate_plan = first["review_packet"]["sets"][0]["candidates"][0]["plan"]
    assert "run_id" not in candidate_plan and "revision" not in candidate_plan
    assert {item["variant"] for item in first["blinding_key"]["assignments"]} == {
        "current",
        "improved",
        "four_role",
    }


def test_missing_coverage_and_invalid_measurements_are_unknown_not_zero() -> None:
    artifact = compare_saved_plans(
        [
            _record("current", 1, measurement={"latency_seconds": float("nan"), "cost_usd": "NaN"}),
            _record("improved", 1, measurement={"latency_seconds": True, "cost_usd": True}),
        ],
        seed="s",
    )

    assert artifact["report"]["comparability"]["comparable"] is False
    assert {item["variant"] for item in artifact["report"]["comparability"]["missing"]} == {
        "four_role"
    }
    measurement = artifact["report"]["measurements_by_variant"]["current"]
    assert measurement["latency_seconds"] == {"measured_count": 0, "unknown_count": 1, "mean": None}
    assert measurement["cost_usd"] == {"measured_count": 0, "unknown_count": 1, "sum": None}


def test_invalid_plan_and_exact_duplicate_are_reported_without_semantic_claims() -> None:
    duplicate = _record("current", 1)
    same_plan = _record("improved", 1)
    invalid = {"variant": "four_role", "object_id": "teacup", "replicate": 1, "plan": {}}
    artifact = compare_saved_plans([duplicate, same_plan, invalid], seed="s")

    assert artifact["report"]["valid_plan_count"] == 2
    assert artifact["report"]["invalid_plans"][0]["reason"] == "invalid_saved_plan"
    assert len(artifact["report"]["exact_duplicate_plan_groups"]) == 1
    assert any("semantic novelty" in item for item in artifact["report"]["limitations"])


def test_invalid_candidates_make_balanced_coverage_noncomparable() -> None:
    records = [
        {"variant": variant, "object_id": "teacup", "replicate": 1, "plan": {}}
        for variant in ("current", "improved", "four_role")
    ]
    artifact = compare_saved_plans(records, seed="s")

    assert artifact["report"]["comparability"]["comparable"] is False
    assert artifact["review_packet"]["sets"] == []
    assert len(artifact["report"]["comparability"]["invalid_candidates"]) == 3


def test_module_writes_review_and_separate_key_report(tmp_path) -> None:
    source = tmp_path / "input.json"
    output = tmp_path / "review.json"
    source.write_text(
        json.dumps({"seed": "s", "records": [_record("current", 1)]}), encoding="utf-8"
    )

    assert main(["--input", str(source), "--output", str(output)]) == 0
    review = json.loads(output.read_text(encoding="utf-8"))
    key = json.loads((tmp_path / "review.key.json").read_text(encoding="utf-8"))
    assert "blinding_key" not in review
    assert "blinding_key" in key and "report" in key
