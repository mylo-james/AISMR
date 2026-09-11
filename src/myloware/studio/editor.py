"""Bounded client for the authenticated monthly Remotion handoff."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from myloware.config import settings
from myloware.tools.inspect_render import _bounded_details
from myloware.tools.remotion import RemotionRenderTool
from myloware.workflows.monthly import MonthlyPlan


class SceneOutput(BaseModel):
    """One approved scene handoff for the renderer-owned calendar assembly."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ordinal: int = Field(ge=1, le=12)
    title: str = Field(min_length=1, max_length=160)
    video_ref: str = Field(min_length=1)
    title_audio_ref: str = Field(min_length=1)


def _validate_scene_outputs(scenes: Sequence[SceneOutput]) -> list[SceneOutput]:
    ordered = list(scenes)
    if len(ordered) != 12 or [scene.ordinal for scene in ordered] != list(range(1, 13)):
        raise ValueError("scenes must be an ordered list with ordinals 1 through 12")
    if len({scene.title.casefold() for scene in ordered}) != 12:
        raise ValueError("scene titles must be distinct")
    return ordered


_RENDER_PHASES = frozenset(
    {
        "queued",
        "preparing",
        "composition",
        "frames",
        "encoding",
        "verification",
        "complete",
        "failed",
    }
)
_STITCH_STAGES = frozenset({"encoding", "muxing"})
_RENDER_COUNTERS = ("total_frames", "rendered_frames", "encoded_frames")


def _safe_render_status(value: object) -> dict[str, Any] | None:
    """Return the renderer's bounded progress receipt, if fully identifiable.

    Progress is optional telemetry. Invalid telemetry must never change the
    verified render result or turn a valid media receipt into a failed run.
    """
    if not isinstance(value, Mapping):
        return None
    schema_version = value.get("schema_version")
    phase = value.get("phase")
    progress = value.get("progress")
    if (
        type(schema_version) is not int
        or schema_version != 1
        or not isinstance(phase, str)
        or phase not in _RENDER_PHASES
        or isinstance(progress, bool)
        or not isinstance(progress, (int, float))
        or not 0.0 <= float(progress) <= 1.0
    ):
        return None
    result: dict[str, Any] = {"phase": phase, "progress": float(progress)}
    for key in _RENDER_COUNTERS:
        counter = value.get(key)
        if type(counter) is int and counter >= 0:
            result[key] = counter
    stitch_stage = value.get("stitch_stage")
    if isinstance(stitch_stage, str) and stitch_stage in _STITCH_STAGES:
        result["stitch_stage"] = stitch_stage
    return result


def _scene_render_provenance(value: object, *, expected_archive_id: str) -> dict[str, Any] | None:
    """Expose the renderer's fixed v2 receipt without generic diagnostic truncation."""
    if not isinstance(value, Mapping) or value.get("render_contract") != "scene-v2":
        return None
    result: dict[str, Any] = {"render_contract": "scene-v2"}
    for key in (
        "render_preset_id",
        "render_preset_sha256",
        "month_bank_id",
        "month_bank_sha256",
        "voice_profile_sha256",
    ):
        if isinstance(value.get(key), str) and value[key]:
            result[key] = value[key]
        else:
            return None
    if isinstance(value.get("join_pause_seconds"), (int, float)):
        result["join_pause_seconds"] = value["join_pause_seconds"]
    hashes = ("assembled_wav_sha256", "title_audio_sha256", "video_sha256")
    for key in hashes:
        items = value.get(key)
        if (
            not isinstance(items, list)
            or len(items) != 12
            or not all(isinstance(item, str) for item in items)
        ):
            return None
        result[key] = list(items)
    profile = value.get("voice_profile")
    if not isinstance(profile, Mapping) or not all(isinstance(key, str) for key in profile):
        return None
    result["voice_profile"] = dict(profile)
    refs = value.get("bank_clip_refs")
    if not isinstance(refs, list) or len(refs) != 12:
        return None
    clean_refs: list[dict[str, Any]] = []
    for index, item in enumerate(refs, start=1):
        if (
            not isinstance(item, Mapping)
            or item.get("ordinal") != index
            or not all(
                isinstance(item.get(key), str) and item[key]
                for key in ("label", "filename", "sha256")
            )
        ):
            return None
        clean_refs.append(
            {
                "ordinal": index,
                "label": item["label"],
                "filename": item["filename"],
                "sha256": item["sha256"],
            }
        )
    result["bank_clip_refs"] = clean_refs
    for key in ("month_bank_source", "month_bank_rights"):
        if isinstance(value.get(key), Mapping):
            result[key] = dict(value[key])
    archive = value.get("narration_archive")
    if archive is not None:
        if (
            not isinstance(archive, Mapping)
            or type(archive.get("schema_version")) is not int
            or archive["schema_version"] != 1
            or archive.get("archive_id") != expected_archive_id
            or not isinstance(archive.get("manifest_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", archive["manifest_sha256"]) is None
            or type(archive.get("file_count")) is not int
            or archive["file_count"] != 12
        ):
            return None
        result["narration_archive"] = {
            "schema_version": archive["schema_version"],
            "archive_id": archive["archive_id"],
            "manifest_sha256": archive["manifest_sha256"],
            "file_count": archive["file_count"],
        }
    return result


def approved_monthly_edit_plan() -> dict[str, Any]:
    """Return the deterministic production preset, without claiming shot selection.

    This preserves the reviewed v3 timing, gentle camera treatment, and mix
    levels while applying the same bounded effect envelope to any approved plan.
    """
    effects = [
        {
            "zoomStart": 1.0 if index % 3 else 1.02,
            "zoomEnd": 1.02 if index % 3 else 1.0,
            "saturation": 1.0,
            "contrast": 1.0,
            "vignette": 0.06,
        }
        for index in range(1, 13)
    ]
    return {
        "segment_frames": [202] * 12,
        "clip_playback_rates": [1.0] * 12,
        "fade_frames": 30,
        "narration_start_frames": [18] * 12,
        "scene_effects": effects,
        "narration_volume": 0.72,
        "music_volume": 0.23,
        "music_ducked_volume": 0.12,
    }


def approved_recorded_edit_plan(archive_root: Path) -> dict[str, Any]:
    """Return the approved v3 edit controls after verifying the archive recipe."""
    from myloware.studio.recorded import RecordedMediaArchive

    archive = RecordedMediaArchive(Path(archive_root))
    # Ensure music itself is hash/size checked before a renderer can receive it.
    archive.music()
    recipe_path = archive.root / "scripts" / "edit-plan.json"
    if not recipe_path.is_file():
        raise ValueError("approved recorded edit plan is missing")
    try:
        recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("approved recorded edit plan is invalid") from exc
    if not isinstance(recipe, dict) or recipe.get("version") != 1:
        raise ValueError("approved recorded edit plan has an unsupported version")
    scenes = recipe.get("scenes")
    if not isinstance(scenes, list) or len(scenes) != 12:
        raise ValueError("approved recorded edit plan needs twelve scenes")
    if (
        recipe.get("segment_frames") != 202
        or recipe.get("fps") != 30
        or recipe.get("fade_frames") != 30
        or recipe.get("narration_start_frames") != 18
    ):
        raise ValueError("approved recorded edit plan violates the v3 timing contract")
    effects = [scene.get("effects") for scene in scenes if isinstance(scene, dict)]
    if len(effects) != 12 or any(not isinstance(effect, dict) for effect in effects):
        raise ValueError("approved recorded edit plan has invalid effects")
    preset = approved_monthly_edit_plan()
    # The generic preset is source-owned. Only recorded mode carries the
    # archived manually reviewed per-scene effect correspondence.
    preset["scene_effects"] = effects
    return preset


class MonthlyEditor:
    def __init__(self, *, timeout: float = 30.0, real_render: bool = True) -> None:
        self._base_url = settings.remotion_service_url.rstrip("/")
        self._secret = settings.remotion_api_secret
        self._timeout = timeout
        self._real_render = real_render

    def _headers(self) -> dict[str, str]:
        return (
            {"Authorization": f"Bearer {self._secret}", "x-api-key": self._secret}
            if self._secret
            else {}
        )

    async def preflight_scenes(self, execution_profile: Mapping[str, Any]) -> None:
        """Confirm the pinned renderer preset and month bank before paid media work."""
        required = {
            "schema_version",
            "pipeline_version",
            "voice_profile",
            "voice_profile_sha256",
            "render_preset_id",
            "render_preset_sha256",
        }
        if not self._real_render or not self._secret or set(execution_profile) != required:
            raise ValueError("scene-v2 renderer preset is unavailable")
        payload = {
            "render_preset": {
                "id": execution_profile["render_preset_id"],
                "sha256": execution_profile["render_preset_sha256"],
            },
            "voice_profile_digest": execution_profile["voice_profile_sha256"],
            "voice_profile": execution_profile["voice_profile"],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/api/render/presets/preflight",
                    json=payload,
                    headers=self._headers(),
                )
                response.raise_for_status()
                if response.json().get("status") != "ready":
                    raise ValueError("scene-v2 renderer preset is unavailable")
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ValueError("scene-v2 renderer preset is unavailable") from exc

    async def submit_scenes(
        self,
        *,
        run_id: str,
        scenes: Sequence[SceneOutput],
        execution_profile: Mapping[str, Any],
        input_hash: str,
        music_url: str | None = None,
        edit_plan: dict[str, Any] | None = None,
        callback_url: str | None = None,
        aspect_ratio: str = "9:16",
    ) -> dict[str, Any]:
        """Submit scene-v2 media without exposing calendar data to the caller.

        The remote renderer resolves the approved preset and performs the only
        ordinal-to-month mapping after it validates the pinned voice profile.
        """
        if not self._real_render:
            return {"status": "fake_unavailable", "media_verified": False}
        if not self._secret:
            return {"status": "rejected", "media_verified": False}
        ordered = _validate_scene_outputs(scenes)
        if (
            not input_hash
            or aspect_ratio != "9:16"
            or (callback_url is not None and not callback_url.strip())
        ):
            return {"status": "rejected", "media_verified": False}
        if not isinstance(execution_profile, Mapping):
            return {"status": "rejected", "media_verified": False}
        required = {
            "schema_version",
            "pipeline_version",
            "voice_profile",
            "voice_profile_sha256",
            "render_preset_id",
            "render_preset_sha256",
        }
        if (
            set(execution_profile) != required
            or execution_profile.get("pipeline_version") != "scene-v2"
        ):
            return {"status": "rejected", "media_verified": False}
        if not isinstance(execution_profile["voice_profile"], Mapping):
            return {"status": "rejected", "media_verified": False}
        RemotionRenderTool._validate_monthly_inputs(
            [scene.video_ref for scene in ordered],
            [scene.title for scene in ordered],
            [scene.title_audio_ref for scene in ordered],
            music_url,
            30,
            aspect_ratio,
        )
        payload: dict[str, Any] = {
            "run_id": run_id,
            "input_hash": input_hash,
            "template": "monthly-scene-v2",
            "ordered_scenes": [scene.model_dump() for scene in ordered],
            "render_preset": {
                "id": execution_profile["render_preset_id"],
                "sha256": execution_profile["render_preset_sha256"],
            },
            "voice_profile_digest": execution_profile["voice_profile_sha256"],
            "voice_profile": dict(execution_profile["voice_profile"]),
            "fps": 30,
            "width": 1080,
            "height": 1920,
        }
        if callback_url is not None:
            payload["callback_url"] = callback_url
        if music_url is not None:
            payload["music_url"] = music_url
        if edit_plan is not None:
            payload["edit_plan"] = edit_plan
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/api/render", json=payload, headers=self._headers()
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            return {
                "status": "rejected" if 400 <= exc.response.status_code < 500 else "unknown",
                "media_verified": False,
            }
        except (httpx.HTTPError, ValueError):
            return {"status": "unknown", "media_verified": False}
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("job_id"), str)
            or data.get("input_hash") != input_hash
        ):
            return {"status": "unknown", "media_verified": False}
        return {
            "status": (
                "accepted"
                if data.get("status") in {"queued", "accepted", "rendering", "done"}
                else "rejected" if data.get("status") == "rejected" else "unknown"
            ),
            "render_job_id": data["job_id"],
            "media_verified": False,
        }

    async def submit(
        self,
        *,
        run_id: str,
        plan: MonthlyPlan,
        video_urls: list[str],
        narration_urls: list[str],
        input_hash: str,
        music_url: str | None = None,
        edit_plan: dict[str, Any] | None = None,
        callback_url: str | None = None,
        aspect_ratio: str = "9:16",
    ) -> dict[str, Any]:
        if not self._real_render:
            return {"status": "fake_unavailable", "media_verified": False}
        if not self._secret:
            # The service permits unauthenticated requests when its secret is absent.
            # Do not let this production adapter rely on that unsafe deployment mode.
            return {"status": "rejected", "media_verified": False}
        if (
            not input_hash
            or aspect_ratio != "9:16"
            or callback_url is not None
            and not callback_url.strip()
        ):
            return {"status": "rejected", "media_verified": False}
        if edit_plan is not None and (
            not isinstance(edit_plan, dict)
            or set(edit_plan)
            - {
                "segment_frames",
                "clip_playback_rates",
                "fade_frames",
                "narration_start_frames",
                "scene_effects",
                "narration_volume",
                "music_volume",
                "music_ducked_volume",
            }
        ):
            return {"status": "rejected", "media_verified": False}
        labels = [idea.label for idea in plan.ideas]
        RemotionRenderTool._validate_monthly_inputs(
            video_urls, labels, narration_urls, music_url, 30, "9:16"
        )
        payload: dict[str, Any] = {
            "run_id": run_id,
            "input_hash": input_hash,
            "template": "monthly",
            "clips": video_urls,
            "objects": labels,
            "narration_urls": narration_urls,
            "fps": 30,
            "width": 1080,
            "height": 1920,
        }
        if callback_url is not None:
            payload["callback_url"] = callback_url
        if music_url is not None:
            payload["music_url"] = music_url
        if edit_plan is not None:
            payload["edit_plan"] = edit_plan
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/api/render",
                    json=payload,
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            return {
                "status": ("rejected" if 400 <= exc.response.status_code < 500 else "unknown"),
                "media_verified": False,
            }
        except (httpx.HTTPError, ValueError):
            return {"status": "unknown", "media_verified": False}
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("job_id"), str)
            or data.get("input_hash") != input_hash
        ):
            return {"status": "unknown", "media_verified": False}
        accepted = data.get("status") in {"queued", "accepted", "rendering", "done"}
        return {
            "status": (
                "accepted"
                if accepted
                else "rejected" if data.get("status") == "rejected" else "unknown"
            ),
            "render_job_id": data["job_id"],
            "media_verified": False,
        }

    async def poll(
        self, *, run_id: str, render_job_id: str, input_hash: str | None = None
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base_url}/api/render/{quote(render_job_id, safe='')}",
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            return {
                "status": ("rejected" if 400 <= exc.response.status_code < 500 else "unknown"),
                "media_verified": False,
            }
        except (httpx.HTTPError, ValueError):
            return {"status": "unknown", "media_verified": False}
        if (
            not isinstance(data, dict)
            or data.get("run_id") != run_id
            or input_hash is not None
            and data.get("input_hash") != input_hash
        ):
            return {"status": "unknown", "media_verified": False}
        status = data.get("status")
        normalized = (
            "ready"
            if status == "done"
            else (
                "failed"
                if status == "error"
                else (
                    "queued"
                    if status == "queued"
                    else ("running" if status in {"running", "rendering"} else "unknown")
                )
            )
        )
        result: dict[str, Any] = {
            "status": normalized,
            "render_job_id": render_job_id,
            "media_verified": bool(
                status == "done"
                and isinstance(data.get("media_diagnostics"), dict)
                and data["media_diagnostics"].get("verified") is True
            ),
        }
        if result["media_verified"] and isinstance(data.get("output_url"), str):
            result["final_url"] = data["output_url"]
        if data.get("video_metadata"):
            result["video_metadata"] = _bounded_details(data["video_metadata"])
        if data.get("media_diagnostics"):
            result["media_diagnostics"] = _bounded_details(data["media_diagnostics"])
            provenance = (
                _scene_render_provenance(
                    data["media_diagnostics"].get("provenance"),
                    expected_archive_id=render_job_id,
                )
                if isinstance(data["media_diagnostics"], Mapping)
                else None
            )
            if provenance is not None:
                result["render_provenance"] = provenance
        render_status = _safe_render_status(data.get("render_status"))
        if render_status is not None:
            result["render_status"] = render_status
        return result
