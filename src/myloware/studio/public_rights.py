"""Offline validation and binding of owner-supplied public-use receipts.

The profile is evidence supplied by the owner.  This module does not retrieve
provider policies, interpret licenses, or create a receipt when evidence is
missing.  Its only job is to bind matching local facts to one verified final.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any


class RightsProfileError(ValueError):
    """The local rights profile is absent, malformed, expired, or mismatched."""


def public_suitability_receipt(*, final_hash: str, moderation: Mapping[str, Any]) -> dict[str, Any]:
    """Bind an already-passed final-media moderation receipt to final bytes."""
    if not _sha256(final_hash) or moderation.get("safe") is not True:
        return {"status": "unverified", "reason": "final_moderation_receipt_missing"}
    fact = {
        "final_hash": final_hash,
        "moderation": _safe_moderation_fact(moderation),
    }
    return {
        "status": "passed",
        "receipt": _digest({"kind": "public_suitability", **fact}),
        "expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
        **fact,
    }


def public_rights_receipt(
    *,
    profile_path: Path | None,
    final_hash: str,
    assets: Iterable[object],
    music_id: str | None,
    music_sha256: str | None,
    now: datetime | None = None,
    render_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a passed receipt only if the configured local profile matches facts."""
    if profile_path is None:
        return {"status": "unverified", "reason": "rights_profile_missing"}
    if not _sha256(final_hash):
        return {"status": "unverified", "reason": "final_hash_invalid"}
    try:
        profile = load_rights_profile(profile_path)
        _require_unexpired(profile, now or datetime.now(UTC))
        actual = source_components(assets)
        _require_provider_match(profile, actual)
        _require_music_match(profile, music_id, music_sha256)
        _require_labels(profile)
        if render_provenance is not None:
            # A listening approval is not a public reuse license. Preserve the
            # source receipt on the private final until an owner supplies rights.
            bank_rights = render_provenance.get("month_bank_rights")
            if not isinstance(bank_rights, Mapping) or not _text(bank_rights.get("reference")):
                raise RightsProfileError("saved_narration_rights_missing")
    except RightsProfileError as exc:
        return {"status": "unverified", "reason": str(exc)}
    fact = {
        "final_hash": final_hash,
        "profile_version": profile["profile_version"],
        "profile_receipt_id": profile["receipt_id"],
        "profile_expires_at": profile["expires_at"].isoformat().replace("+00:00", "Z"),
        "provider_policy_reference": profile["provider_policy"]["reference"],
        "music_license_reference": profile["music"]["license_reference"],
        "music_attribution": profile["music"]["attribution"],
        "labels_permission_reference": profile["labels"]["permission_reference"],
        "labels_attribution": profile["labels"]["attribution"],
        "source_models": actual,
        **(
            {
                "saved_narration": {
                    "bank_sha256": render_provenance.get("month_bank_sha256"),
                    "source": render_provenance.get("month_bank_source"),
                    "rights": render_provenance.get("month_bank_rights"),
                }
            }
            if render_provenance is not None
            else {}
        ),
    }
    return {
        "status": "passed",
        "receipt": _digest({"kind": "public_rights", **fact}),
        **fact,
    }


def load_rights_profile(path: Path) -> dict[str, Any]:
    """Load and validate the version-one owner profile without external lookups."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RightsProfileError("rights_profile_unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RightsProfileError("rights_profile_schema_invalid")
    for field in ("profile_version", "receipt_id"):
        if not _text(payload.get(field)):
            raise RightsProfileError("rights_profile_schema_invalid")
    expires_at = _parse_timestamp(payload.get("expires_at"))
    provider_policy = payload.get("provider_policy")
    music = payload.get("music")
    labels = payload.get("labels")
    if (
        not isinstance(provider_policy, dict)
        or not isinstance(music, dict)
        or not isinstance(labels, dict)
    ):
        raise RightsProfileError("rights_profile_schema_invalid")
    if not _text(provider_policy.get("reference")):
        raise RightsProfileError("rights_profile_schema_invalid")
    for field in ("permitted_video_model_ids", "permitted_narration_model_ids"):
        values = provider_policy.get(field)
        if not isinstance(values, list) or not values or not all(_text(value) for value in values):
            raise RightsProfileError("rights_profile_schema_invalid")
    if (
        not _text(music.get("id"))
        or not _sha256(music.get("sha256"))
        or not _text(music.get("license_reference"))
        or not _text(music.get("attribution"))
        or not _text(labels.get("permission_reference"))
        or not _text(labels.get("attribution"))
    ):
        raise RightsProfileError("rights_profile_schema_invalid")
    return {
        "schema_version": 1,
        "profile_version": payload["profile_version"].strip(),
        "receipt_id": payload["receipt_id"].strip(),
        "expires_at": expires_at,
        "provider_policy": {
            "reference": provider_policy["reference"].strip(),
            "permitted_video_model_ids": tuple(provider_policy["permitted_video_model_ids"]),
            "permitted_narration_model_ids": tuple(
                provider_policy["permitted_narration_model_ids"]
            ),
        },
        "music": {
            "id": music["id"].strip(),
            "sha256": music["sha256"],
            "license_reference": music["license_reference"].strip(),
            "attribution": music["attribution"].strip(),
        },
        "labels": {
            "permission_reference": labels["permission_reference"].strip(),
            "attribution": labels["attribution"].strip(),
        },
    }


def source_components(assets: Iterable[object]) -> dict[str, list[str]]:
    """Extract actual ready video and narration models from persisted run assets."""
    videos: list[str] = []
    narration: list[str] = []
    for asset in assets:
        kind = _value(asset, "kind")
        if _value(asset, "status") != "ready":
            continue
        model = _value(asset, "model")
        if not _text(model):
            continue
        if kind == "video":
            videos.append(model)
        elif kind == "voice_batch":
            narration.append(model)
    if len(videos) != 12 or len(narration) != 1:
        raise RightsProfileError("source_components_incomplete")
    return {
        "video_model_ids": sorted(set(videos)),
        "narration_model_ids": sorted(set(narration)),
    }


def _require_unexpired(profile: Mapping[str, Any], now: datetime) -> None:
    expiry = profile["expires_at"]
    if not isinstance(expiry, datetime) or expiry <= now.astimezone(UTC):
        raise RightsProfileError("rights_profile_expired")


def _require_provider_match(profile: Mapping[str, Any], actual: Mapping[str, list[str]]) -> None:
    policy = profile["provider_policy"]
    if not isinstance(policy, Mapping):
        raise RightsProfileError("rights_profile_schema_invalid")
    if not set(actual["video_model_ids"]).issubset(policy["permitted_video_model_ids"]):
        raise RightsProfileError("video_provider_not_permitted")
    if not set(actual["narration_model_ids"]).issubset(policy["permitted_narration_model_ids"]):
        raise RightsProfileError("narration_provider_not_permitted")


def _require_music_match(
    profile: Mapping[str, Any], music_id: str | None, music_sha256: str | None
) -> None:
    music = profile["music"]
    if not isinstance(music, Mapping) or music_id is None or music_sha256 is None:
        raise RightsProfileError("music_receipt_missing")
    if music.get("id") != music_id or music.get("sha256") != music_sha256:
        raise RightsProfileError("music_receipt_mismatch")


def _require_labels(profile: Mapping[str, Any]) -> None:
    labels = profile["labels"]
    if not isinstance(labels, Mapping) or not _text(labels.get("permission_reference")):
        raise RightsProfileError("labels_permission_missing")


def _safe_moderation_fact(moderation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "safe": True,
        "model": _text(moderation.get("model")) or None,
        "coverage": [value for value in moderation.get("coverage", ()) if _text(value)],
    }


def _parse_timestamp(value: object) -> datetime:
    if not _text(value):
        raise RightsProfileError("rights_profile_schema_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RightsProfileError("rights_profile_schema_invalid") from exc
    if parsed.tzinfo is None:
        raise RightsProfileError("rights_profile_schema_invalid")
    return parsed.astimezone(UTC)


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _value(asset: object, field: str) -> str:
    if isinstance(asset, Mapping):
        return _text(asset.get(field))
    return _text(getattr(asset, field, None))
