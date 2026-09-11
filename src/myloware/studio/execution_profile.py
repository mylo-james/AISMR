"""Immutable scene execution inputs, separate from creative-agent context."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VOICE_ROOT = Path(__file__).resolve().parents[3] / "data" / "media" / "voice"
VOICE_PROFILE_ID = "eleven-v3-whisper-en-v1"
RENDER_PRESET_ID = "monthly-saved-voice-v2"


class VoiceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_version: str = Field(min_length=1, max_length=96)
    model: Literal["fal-ai/elevenlabs/tts/eleven-v3"]
    voice: str = Field(min_length=1, max_length=128)
    stability: float = Field(ge=0, le=1)
    language_code: Literal["en"]
    apply_text_normalization: Literal["off"]
    timestamps: Literal[True]
    delivery_prefix: Literal["[whispers]"]
    between_cues: Literal["[long pause]"]


def voice_profile_digest(profile: Mapping[str, Any] | VoiceProfile) -> str:
    validated = VoiceProfile.model_validate(profile)
    return sha256(
        json.dumps(
            validated.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


class ExecutionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    pipeline_version: Literal["scene-v2"]
    voice_profile: VoiceProfile
    voice_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_preset_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,95}$")
    render_preset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_matching_profile(self) -> ExecutionProfile:
        if voice_profile_digest(self.voice_profile) != self.voice_profile_sha256:
            raise ValueError("voice_profile_digest_mismatch")
        return self


def validate_execution_profile(value: object) -> dict[str, Any]:
    """Validate persisted identity without silently loading mutable defaults."""
    return ExecutionProfile.model_validate(value).model_dump(mode="json")


def build_execution_profile(*, voice_root: Path = VOICE_ROOT) -> dict[str, Any]:
    """Pin the shipped voice and opaque preset when admitting a new scene run."""
    voice = VoiceProfile.model_validate_json(
        (voice_root / "profiles" / f"{VOICE_PROFILE_ID}.json").read_text()
    )
    preset_bytes = (voice_root / "presets" / f"{RENDER_PRESET_ID}.json").read_bytes()
    preset = json.loads(preset_bytes)
    profile_hash = voice_profile_digest(voice)
    if (
        preset.get("schema_version") != 1
        or preset.get("id") != RENDER_PRESET_ID
        or preset.get("render_contract") != "scene-v2"
        or preset.get("voice_profile_sha256") != profile_hash
    ):
        raise ValueError("render_preset_profile_mismatch")
    return validate_execution_profile(
        {
            "schema_version": 1,
            "pipeline_version": "scene-v2",
            "voice_profile": voice.model_dump(mode="json"),
            "voice_profile_sha256": profile_hash,
            "render_preset_id": RENDER_PRESET_ID,
            "render_preset_sha256": sha256(preset_bytes).hexdigest(),
        }
    )


def require_plan_execution(plan: Any, profile: object) -> dict[str, Any] | None:
    """Keep legacy hashes/receipts on their pipeline; never coerce between versions."""
    if plan.schema_version == 1:
        if profile is not None:
            raise ValueError("plan_execution_version_mismatch")
        return None
    if plan.schema_version != 2 or profile is None:
        raise ValueError("plan_execution_version_mismatch")
    return validate_execution_profile(profile)
