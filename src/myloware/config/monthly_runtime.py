"""Offline contract for the AISMR monthly Llama Stack runtime profile.

This module is deliberately provider-call free.  It records the finite limits
and response contracts that later configuration and graph work must enforce.
It does not make a saved Codex login, an environment credential, or fake-mode
success evidence of a live capability.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MONTHLY_RUNTIME_PROFILE_NAME = "aismr-scenes-v2"
MONTHLY_MODEL_ID = "openai/gpt-4o-mini"
MONTHLY_SHIELD_ID = "content_safety"


class MonthlyRuntimeConfigurationError(ValueError):
    """Raised when a monthly runtime contract is incomplete or unsafe."""


class SafetyFixtureOutcome(StrEnum):
    """Fail-closed outcomes for deterministic safety fixture responses."""

    ALLOW = "allow"
    DENY = "deny"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class MonthlyRuntimeProfile:
    """Finite limits and registered resource identifiers for monthly ideation."""

    name: str
    model_id: str
    shield_id: str
    max_model_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_tool_calls: int
    allowed_tools: tuple[str, ...]
    response_schema: Mapping[str, Any]


@dataclass(frozen=True)
class SafetyFixture:
    """A static response shape used to prove offline safety handling."""

    name: str
    response: Mapping[str, Any] | None = None
    outage: str | None = None


@dataclass(frozen=True)
class LiveCapabilityStatus:
    """Observed prerequisites supplied by a future live startup check.

    The values are observations, not inferred from the presence of a profile
    file.  Credentials are represented only by a boolean so callers never
    pass a secret through this API.
    """

    provider_mode: str
    inference_credentials_configured: bool
    stack_reachable: bool
    model_registered: bool
    shield_registered: bool
    structured_output_supported: bool
    allowed_tools_supported: bool


MONTHLY_IDEATION_RESPONSE_SCHEMA: dict[str, Any] = {
    "name": "aismr_scene_ideas_v2",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["ideas"],
        "properties": {
            "ideas": {
                "type": "array",
                "minItems": 12,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "ordinal",
                        "title",
                        "visual_prompt",
                    ],
                    "properties": {
                        "ordinal": {"type": "integer", "minimum": 1, "maximum": 12},
                        "title": {"type": "string", "minLength": 1, "maxLength": 48},
                        "visual_prompt": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1200,
                        },
                    },
                },
            }
        },
    },
}


MONTHLY_RUNTIME_PROFILE = MonthlyRuntimeProfile(
    name=MONTHLY_RUNTIME_PROFILE_NAME,
    model_id=MONTHLY_MODEL_ID,
    shield_id=MONTHLY_SHIELD_ID,
    max_model_calls=1,
    max_input_tokens=6000,
    max_output_tokens=2400,
    max_tool_calls=3,
    allowed_tools=("role_knowledge_search", "builtin::websearch"),
    response_schema=MONTHLY_IDEATION_RESPONSE_SCHEMA,
)


OFFLINE_SAFETY_FIXTURES: tuple[SafetyFixture, ...] = (
    SafetyFixture(name="allow", response={"violation": None}),
    SafetyFixture(
        name="deny",
        response={"violation": {"user_message": "Content policy violation"}},
    ),
    SafetyFixture(name="malformed", response={"safe": True}),
    SafetyFixture(name="outage", outage="safety provider unavailable"),
)


def evaluate_safety_fixture(fixture: SafetyFixture) -> SafetyFixtureOutcome:
    """Classify a Llama Stack safety fixture without calling a provider.

    Only an explicit ``violation: null`` permits content.  Missing or malformed
    data and provider outages fail closed, matching the required live behavior.
    """

    if fixture.outage:
        return SafetyFixtureOutcome.UNAVAILABLE
    if fixture.response is None or set(fixture.response) != {"violation"}:
        return SafetyFixtureOutcome.MALFORMED

    violation = fixture.response["violation"]
    if violation is None:
        return SafetyFixtureOutcome.ALLOW
    if isinstance(violation, Mapping) and isinstance(violation.get("user_message"), str):
        return SafetyFixtureOutcome.DENY
    return SafetyFixtureOutcome.MALFORMED


def validate_monthly_runtime_profile(profile: MonthlyRuntimeProfile) -> None:
    """Reject profiles that could make unbounded or unstructured ideation calls."""

    if profile.name != MONTHLY_RUNTIME_PROFILE_NAME:
        raise MonthlyRuntimeConfigurationError("monthly runtime profile name is not recognized")
    if profile.model_id != MONTHLY_MODEL_ID:
        raise MonthlyRuntimeConfigurationError(
            "monthly runtime must use the registered small model"
        )
    if profile.shield_id != MONTHLY_SHIELD_ID:
        raise MonthlyRuntimeConfigurationError("monthly runtime must use content_safety")
    if profile.max_model_calls != 1:
        raise MonthlyRuntimeConfigurationError("monthly ideation permits exactly one model call")
    if profile.max_input_tokens <= 0 or profile.max_output_tokens <= 0:
        raise MonthlyRuntimeConfigurationError("monthly runtime token caps must be positive")
    if profile.max_tool_calls < 0:
        raise MonthlyRuntimeConfigurationError("monthly runtime tool cap cannot be negative")
    if not profile.allowed_tools or len(set(profile.allowed_tools)) != len(profile.allowed_tools):
        raise MonthlyRuntimeConfigurationError("monthly runtime tool allowlist is invalid")

    schema = profile.response_schema
    if schema.get("name") != "aismr_scene_ideas_v2" or schema.get("strict") is not True:
        raise MonthlyRuntimeConfigurationError("monthly runtime requires the strict scene schema")
    body = schema.get("schema")
    if not isinstance(body, Mapping):
        raise MonthlyRuntimeConfigurationError("monthly runtime schema body is missing")
    ideas = body.get("properties", {}).get("ideas")
    if not isinstance(ideas, Mapping) or ideas.get("minItems") != 12 or ideas.get("maxItems") != 12:
        raise MonthlyRuntimeConfigurationError(
            "monthly runtime schema must require exactly twelve ideas"
        )


def require_live_capability_checks(status: LiveCapabilityStatus) -> None:
    """Require observed real-mode capability before a future live enablement.

    This is intentionally not a credential checker and it does not call a
    service.  Startup integration must provide observations from real checks.
    """

    failures: list[str] = []
    if status.provider_mode != "real":
        failures.append("LLAMA_STACK_PROVIDER must be real")
    if not status.inference_credentials_configured:
        failures.append("inference credentials are not configured")
    if not status.stack_reachable:
        failures.append("Llama Stack is unreachable")
    if not status.model_registered:
        failures.append(f"model {MONTHLY_MODEL_ID} is not registered")
    if not status.shield_registered:
        failures.append(f"shield {MONTHLY_SHIELD_ID} is not registered")
    if not status.structured_output_supported:
        failures.append("structured output is not confirmed")
    if not status.allowed_tools_supported:
        failures.append("monthly tool allowlist is not confirmed")
    if failures:
        raise MonthlyRuntimeConfigurationError("; ".join(failures))
