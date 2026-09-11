"""Pure, fail-closed repair contracts for creative-planning model output."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_REPAIR_ATTEMPTS = 2
REPAIR_OPERATION_NAMES = ("repair_1", "repair_2")
REPAIR_PROMPT_VERSION = "creative-repair-v1"
REPAIR_SCHEMA_VERSION = 1


class CreativeRepairError(ValueError):
    """A bounded repair could not safely reconstruct a source-role result."""

    def __init__(
        self,
        code: str = "creative_repair_exhausted",
        *,
        source_role: str | None = None,
        attempt: int | None = None,
        issues: tuple[RepairIssue, ...] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.source_role = source_role
        self.attempt = attempt
        self.issues = issues


class RepairIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_role: str = Field(min_length=1, max_length=48)
    record_index: int | None = Field(default=None, ge=0, le=44)
    field: str = Field(min_length=1, max_length=80)
    rule: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=500)


class RepairPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1, max_length=160)
    value: str | int | float | bool | None


class RepairResponse(BaseModel):
    """Patch only known-invalid fields, or replace a wholly unreliable source result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_role: str = Field(min_length=1, max_length=48)
    mode: Literal["patch", "append", "replace"]
    patches: tuple[RepairPatch, ...] = Field(default=(), max_length=45)
    append: dict[str, Any] | None = None
    replacement: dict[str, Any] | None = None

    @field_validator("patches")
    @classmethod
    def require_distinct_targets(cls, value: tuple[RepairPatch, ...]) -> tuple[RepairPatch, ...]:
        if len({patch.target for patch in value}) != len(value):
            raise ValueError("repair patch targets must be unique")
        return value


def source_response_sha256(value: Mapping[str, Any] | str) -> str:
    """Canonical identity of the original received model output."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            encoded = value.encode("utf-8")
        else:
            encoded = json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
    else:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    return sha256(encoded).hexdigest()


def issue_target(issue: RepairIssue) -> str:
    if issue.record_index is None:
        return issue.field
    return f"records[{issue.record_index}].{issue.field}"


def apply_patches(
    original: Mapping[str, Any], *, allowed_targets: set[str], patches: tuple[RepairPatch, ...]
) -> dict[str, Any]:
    """Apply only exact invalid record fields, preserving every valid sibling field."""
    if {patch.target for patch in patches} - allowed_targets:
        raise CreativeRepairError("repair_unauthorized_patch_target")
    value = json.loads(json.dumps(original))
    for patch in patches:
        prefix, separator, field = patch.target.partition("].")
        if not separator or not prefix.startswith("records[") or not field:
            raise CreativeRepairError("repair_invalid_patch_target")
        try:
            index = int(prefix.removeprefix("records["))
        except ValueError as exc:
            raise CreativeRepairError("repair_invalid_patch_target") from exc
        records = value.get("records")
        if not isinstance(records, list) or index not in range(len(records)):
            raise CreativeRepairError("repair_invalid_patch_target")
        record = records[index]
        if not isinstance(record, dict):
            raise CreativeRepairError("repair_invalid_patch_target")
        parent = record
        parts = field.split(".")
        for part in parts[:-1]:
            child = parent.get(part)
            if not isinstance(child, dict):
                raise CreativeRepairError("repair_invalid_patch_target")
            parent = child
        parent[parts[-1]] = patch.value
    return value
