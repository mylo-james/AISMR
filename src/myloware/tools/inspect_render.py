"""Bounded status and quality inspection for an editor's render job."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from myloware.config import settings
from myloware.config.provider_modes import effective_remotion_provider
from myloware.tools.base import JSONSchema, MylowareBaseTool, format_tool_error, format_tool_success

_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_STATUS_FIELDS = ("status", "progress", "template")
_MAX_DIAGNOSTIC_FIELDS = 16
_MAX_VALUE_CHARS = 500
_MAX_SEGMENTS = 12


def _bounded_details(value: object, depth: int = 0) -> Any:
    """Keep service diagnostics useful without returning arbitrary response data."""
    if isinstance(value, str):
        return value[:_MAX_VALUE_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        if depth >= 2:
            return [
                _bounded_leaf_mapping(item)
                for item in value[:_MAX_SEGMENTS]
                if isinstance(item, dict)
            ]
        return [
            entry_value
            for item in value[:_MAX_SEGMENTS]
            if (entry_value := _bounded_details(item, depth + 1)) is not None
        ]
    if not isinstance(value, dict):
        return {}
    bounded: dict[str, Any] = {}
    for key, item in list(value.items())[:_MAX_DIAGNOSTIC_FIELDS]:
        if not isinstance(key, str):
            continue
        if depth >= 2:
            if isinstance(item, str):
                bounded[key] = item[:_MAX_VALUE_CHARS]
            elif isinstance(item, (int, float, bool)) or item is None:
                bounded[key] = item
            continue
        result = _bounded_details(item, depth + 1)
        if result is not None:
            bounded[key] = result
    return bounded


def _bounded_leaf_mapping(value: dict[object, object]) -> dict[str, Any]:
    """Return scalar source metadata for one bounded diagnostic segment."""
    return {
        key: item[:_MAX_VALUE_CHARS] if isinstance(item, str) else item
        for key, item in list(value.items())[:_MAX_DIAGNOSTIC_FIELDS]
        if isinstance(key, str) and (isinstance(item, (str, int, float, bool)) or item is None)
    }


def _scene_render_provenance(value: object) -> dict[str, Any] | None:
    """Preserve the fixed v2 receipt separately from generic diagnostics."""
    if not isinstance(value, dict) or value.get("render_contract") != "scene-v2":
        return None
    required_scalars = (
        "render_preset_id",
        "render_preset_sha256",
        "month_bank_id",
        "month_bank_sha256",
        "voice_profile_sha256",
    )
    if not all(isinstance(value.get(key), str) and value[key] for key in required_scalars):
        return None
    result: dict[str, Any] = {key: value[key] for key in required_scalars}
    result["render_contract"] = "scene-v2"
    for key in ("assembled_wav_sha256", "title_audio_sha256", "video_sha256"):
        entries = value.get(key)
        if (
            not isinstance(entries, list)
            or len(entries) != 12
            or not all(isinstance(item, str) for item in entries)
        ):
            return None
        result[key] = list(entries)
    profile = value.get("voice_profile")
    refs = value.get("bank_clip_refs")
    if not isinstance(profile, dict) or not isinstance(refs, list) or len(refs) != 12:
        return None
    result["voice_profile"] = dict(profile)
    clean_refs: list[dict[str, Any]] = []
    for ordinal, ref in enumerate(refs, start=1):
        if (
            not isinstance(ref, dict)
            or ref.get("ordinal") != ordinal
            or not all(
                isinstance(ref.get(key), str) and ref[key]
                for key in ("label", "filename", "sha256")
            )
        ):
            return None
        clean_refs.append(
            {
                "ordinal": ordinal,
                "label": ref["label"],
                "filename": ref["filename"],
                "sha256": ref["sha256"],
            }
        )
    result["bank_clip_refs"] = clean_refs
    if isinstance(value.get("join_pause_seconds"), (int, float)):
        result["join_pause_seconds"] = value["join_pause_seconds"]
    for key in ("month_bank_source", "month_bank_rights"):
        if isinstance(value.get(key), dict):
            result[key] = dict(value[key])
    return result


class InspectRenderTool(MylowareBaseTool):
    """Inspect one render job belonging to the run fixed at construction time."""

    def __init__(self, *, run_id: str | None, timeout: float = 10.0) -> None:
        self._run_id = run_id
        self._timeout = timeout
        self._provider_mode = effective_remotion_provider(settings)
        self._base_url = getattr(settings, "remotion_service_url", "")
        self._api_secret = getattr(settings, "remotion_api_secret", "")
        if self._provider_mode == "off":
            raise ValueError("Remotion provider is disabled (REMOTION_PROVIDER=off)")
        if self._provider_mode == "real" and not self._run_id:
            raise ValueError("InspectRenderTool requires a bound run_id in real mode")
        if self._provider_mode == "real" and not self._base_url:
            raise ValueError("REMOTION_SERVICE_URL must be configured")

    def get_name(self) -> str:
        return "inspect_render"

    def get_description(self) -> str:
        return (
            "Inspect the current status and service-reported diagnostics for this run's render job."
        )

    def get_input_schema(self) -> JSONSchema:
        return {
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "Render job identifier"}},
            "required": ["job_id"],
        }

    async def async_run_impl(self, job_id: str) -> dict[str, Any]:
        if not isinstance(job_id, str) or not _JOB_ID_PATTERN.fullmatch(job_id):
            return format_tool_error("invalid_render_job", "job_id must be a render job identifier")
        if self._provider_mode == "fake":
            return format_tool_success(
                {
                    "job_id": job_id,
                    "status": "fake_unavailable",
                    "media_verified": False,
                    "service": "remotion",
                },
                message="Fake mode cannot verify render job media",
            )

        headers = {}
        if self._api_secret:
            headers = {"Authorization": f"Bearer {self._api_secret}", "x-api-key": self._api_secret}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base_url}/api/render/{quote(job_id, safe='')}", headers=headers
                )
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException:
            return format_tool_error(
                "render_status_unavailable", "Render service status request timed out"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return format_tool_error("render_job_not_found", "Render job was not found")
            return format_tool_error(
                "render_status_unavailable", "Render service status request failed"
            )
        except (httpx.HTTPError, ValueError):
            return format_tool_error(
                "render_status_unavailable", "Render service status request failed"
            )

        if not isinstance(data, dict) or data.get("run_id") != self._run_id:
            return format_tool_error(
                "render_job_run_mismatch", "Render job does not belong to this run"
            )

        result: dict[str, Any] = {"job_id": job_id, "service": "remotion"}
        for field in _STATUS_FIELDS:
            if field in data and isinstance(data[field], (str, int, float)):
                result[field] = data[field]
        if data.get("status") == "done" and isinstance(data.get("output_url"), str):
            result["final_artifact_url"] = data["output_url"]
        video_metadata = _bounded_details(data.get("video_metadata"))
        media_diagnostics = _bounded_details(data.get("media_diagnostics"))
        if video_metadata:
            result["video_metadata"] = video_metadata
        if media_diagnostics:
            result["media_diagnostics"] = media_diagnostics
        if isinstance(data.get("media_diagnostics"), dict):
            provenance = _scene_render_provenance(data["media_diagnostics"].get("provenance"))
            if provenance is not None:
                result["render_provenance"] = provenance
        result["media_verified"] = bool(
            data.get("status") == "done"
            and isinstance(data.get("media_diagnostics"), dict)
            and data["media_diagnostics"].get("verified") is True
        )
        if data.get("status") == "error" and isinstance(data.get("error"), str):
            result["error"] = data["error"][:_MAX_VALUE_CHARS]
        return format_tool_success(result, message="Render service status retrieved")
