"""Offline tests for the AISMR monthly Llama Stack runtime contract."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from myloware.config.monthly_runtime import (
    MONTHLY_IDEATION_RESPONSE_SCHEMA,
    MONTHLY_MODEL_ID,
    MONTHLY_RUNTIME_PROFILE,
    MONTHLY_SHIELD_ID,
    OFFLINE_SAFETY_FIXTURES,
    LiveCapabilityStatus,
    MonthlyRuntimeConfigurationError,
    SafetyFixtureOutcome,
    evaluate_safety_fixture,
    require_live_capability_checks,
    validate_monthly_runtime_profile,
)


def test_monthly_runtime_profile_is_finite_and_structured() -> None:
    validate_monthly_runtime_profile(MONTHLY_RUNTIME_PROFILE)

    assert MONTHLY_RUNTIME_PROFILE.max_model_calls == 1
    assert MONTHLY_RUNTIME_PROFILE.max_input_tokens == 6000
    assert MONTHLY_RUNTIME_PROFILE.max_output_tokens == 2400
    assert MONTHLY_RUNTIME_PROFILE.max_tool_calls == 3
    assert MONTHLY_RUNTIME_PROFILE.response_schema is MONTHLY_IDEATION_RESPONSE_SCHEMA

    ideas = MONTHLY_IDEATION_RESPONSE_SCHEMA["schema"]["properties"]["ideas"]
    assert ideas["minItems"] == 12
    assert ideas["maxItems"] == 12
    assert MONTHLY_IDEATION_RESPONSE_SCHEMA["strict"] is True


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("allow", SafetyFixtureOutcome.ALLOW),
        ("deny", SafetyFixtureOutcome.DENY),
        ("malformed", SafetyFixtureOutcome.MALFORMED),
        ("outage", SafetyFixtureOutcome.UNAVAILABLE),
    ],
)
def test_offline_safety_fixtures_fail_closed(
    fixture_name: str, expected: SafetyFixtureOutcome
) -> None:
    fixture = next(item for item in OFFLINE_SAFETY_FIXTURES if item.name == fixture_name)

    assert evaluate_safety_fixture(fixture) is expected
    assert (expected is SafetyFixtureOutcome.ALLOW) is (fixture_name == "allow")


def test_live_runtime_requires_observed_real_capabilities() -> None:
    with pytest.raises(MonthlyRuntimeConfigurationError, match="LLAMA_STACK_PROVIDER"):
        require_live_capability_checks(
            LiveCapabilityStatus(
                provider_mode="fake",
                inference_credentials_configured=False,
                stack_reachable=False,
                model_registered=False,
                shield_registered=False,
                structured_output_supported=False,
                allowed_tools_supported=False,
            )
        )

    require_live_capability_checks(
        LiveCapabilityStatus(
            provider_mode="real",
            inference_credentials_configured=True,
            stack_reachable=True,
            model_registered=True,
            shield_registered=True,
            structured_output_supported=True,
            allowed_tools_supported=True,
        )
    )


def test_monthly_profile_yaml_registers_the_same_model_and_shield() -> None:
    profile_path = Path(__file__).parents[2] / "llama_stack" / "monthly-profile.yaml"
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))

    assert profile["version"] == 2
    assert "safety" in profile["apis"]
    assert profile["models"] == [{"model_id": MONTHLY_MODEL_ID, "provider_id": "openai"}]
    assert profile["registered_resources"]["shields"] == [
        {"shield_id": MONTHLY_SHIELD_ID, "provider_id": "llama-guard"}
    ]
