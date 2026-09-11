"""Bind scene media and renderer receipts to the approved plan and voice profile."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from myloware.providers.media import FAKE_NARRATION_BATCH_MODEL, LOCAL_SCENE_MEDIA_MODEL
from myloware.storage.studio_models import StudioAsset
from myloware.studio.voice_batch import build_scene_narration_text, scene_narration_lines
from myloware.workflows.scenes import ScenePlan


def validate_scene_assets(
    plan: ScenePlan,
    profile: Mapping[str, Any],
    assets: Sequence[StudioAsset],
    *,
    mode: str = "live",
) -> dict[tuple[int, str], StudioAsset]:
    """Reject stale or mixed title/video receipts before projecting render inputs."""
    ready = sorted(
        (asset for asset in assets if asset.status == "ready"), key=lambda asset: asset.attempt
    )
    selected = {(int(asset.ordinal), str(asset.kind)): asset for asset in ready}
    if mode == "local":
        return _validate_local_scene_assets(plan, profile, selected)
    lines = scene_narration_lines(plan)
    text = build_scene_narration_text(
        lines,
        delivery_cue=profile["voice_profile"]["delivery_prefix"],
        between_cues=profile["voice_profile"]["between_cues"],
    )
    batch = selected.get((0, "voice_batch"))
    expected_model = (
        FAKE_NARRATION_BATCH_MODEL if mode == "fixture" else profile["voice_profile"]["model"]
    )
    metadata = dict(batch.media_metadata or {}) if batch else {}
    if (
        batch is None
        or batch.model != expected_model
        or not isinstance(batch.request_id, str)
        or not batch.request_id.strip()
        or batch.revision != plan.revision
        or batch.run_id != plan.run_id
        or batch.input_hash != sha256("\n".join(lines).encode()).hexdigest()
        or not batch.sha256
        or metadata.get("voice_profile") != profile["voice_profile"]
        or metadata.get("voice_profile_sha256") != profile["voice_profile_sha256"]
        or metadata.get("submitted_text") != text
        or metadata.get("submitted_text_sha256") != sha256(text.encode()).hexdigest()
        or metadata.get("timestamps_verified") is not True
        or not batch.safety
        or batch.safety.get("safe") is not True
        or batch.safety.get("media_sha256") != batch.sha256
    ):
        raise ValueError("scene_narration_provenance_mismatch")
    for scene, line in zip(plan.scenes, lines, strict=True):
        for kind, input_text in (("video", scene.visual_prompt), ("voice", line)):
            asset = selected.get((scene.ordinal, kind))
            if (
                asset is None
                or asset.run_id != plan.run_id
                or asset.revision != plan.revision
                or asset.input_hash != sha256(input_text.encode()).hexdigest()
                or not asset.artifact_id
                or not asset.sha256
                or not asset.safety
                or asset.safety.get("safe") is not True
                or asset.safety.get("media_sha256") != asset.sha256
            ):
                raise ValueError("scene_asset_approval_mismatch")
            asset_metadata: dict[str, Any] = dict(asset.media_metadata or {})
            if kind == "voice" and (
                asset_metadata.get("batch_sha256") != batch.sha256
                or asset.request_id != batch.request_id
                or asset.model != batch.model
            ):
                raise ValueError("scene_title_batch_mismatch")
    return {
        (ordinal, kind): selected[(ordinal, kind)]
        for ordinal in range(1, 13)
        for kind in ("video", "voice")
    }


def _validate_local_scene_assets(
    plan: ScenePlan,
    profile: Mapping[str, Any],
    selected: Mapping[tuple[int, str], StudioAsset],
) -> dict[tuple[int, str], StudioAsset]:
    """Accept only an explicit private replay mismatch, never a fabricated match."""
    requested_lines = scene_narration_lines(plan)
    batch = selected.get((0, "voice_batch"))
    metadata = dict(batch.media_metadata or {}) if batch else {}
    source_lines = metadata.get("source_title_lines")
    receipt = metadata.get("local_media_receipt")
    if (
        batch is None
        or batch.model != LOCAL_SCENE_MEDIA_MODEL
        or not isinstance(batch.request_id, str)
        or not batch.request_id.strip()
        or batch.run_id != plan.run_id
        or batch.revision != plan.revision
        or batch.input_hash != sha256("\n".join(requested_lines).encode()).hexdigest()
        or not batch.sha256
        or metadata.get("alignment_status") != "ordinal_only_mismatch"
        or metadata.get("requested_title_lines") != list(requested_lines)
        or metadata.get("requested_title_lines_sha256")
        != sha256("\n".join(requested_lines).encode()).hexdigest()
        or not isinstance(source_lines, list)
        or len(source_lines) != 12
        or not all(isinstance(line, str) and line for line in source_lines)
        or metadata.get("source_title_text_sha256")
        != sha256("\n\n".join(source_lines).encode()).hexdigest()
        or not isinstance(receipt, Mapping)
        or not isinstance(receipt.get("manifest_sha256"), str)
        or receipt.get("local_batch_sha256") != batch.sha256
        or metadata.get("voice_profile") != profile["voice_profile"]
        or metadata.get("voice_profile_sha256") != profile["voice_profile_sha256"]
        or metadata.get("timestamps_verified") is not True
        or metadata.get("source_batch_sha256") != batch.sha256
        or not batch.safety
        or batch.safety.get("safe") is not True
        or batch.safety.get("media_sha256") != batch.sha256
    ):
        raise ValueError("local_scene_narration_provenance_mismatch")
    for scene, requested_line, source_line in zip(
        plan.scenes, requested_lines, source_lines, strict=True
    ):
        for kind, requested_hash in (
            ("video", sha256(scene.visual_prompt.encode()).hexdigest()),
            ("voice", sha256(requested_line.encode()).hexdigest()),
        ):
            asset = selected.get((scene.ordinal, kind))
            asset_metadata = dict(asset.media_metadata or {}) if asset else {}
            if (
                asset is None
                or asset.model != LOCAL_SCENE_MEDIA_MODEL
                or asset.run_id != plan.run_id
                or asset.revision != plan.revision
                or asset.input_hash != requested_hash
                or not asset.artifact_id
                or not asset.sha256
                or not asset.safety
                or asset.safety.get("safe") is not True
                or asset.safety.get("media_sha256") != asset.sha256
                or asset_metadata.get("alignment_status") != "ordinal_only_mismatch"
                or asset_metadata.get("source_title") != source_line
                or asset_metadata.get("source_title_sha256")
                != sha256(source_line.encode()).hexdigest()
                or asset_metadata.get("local_media_receipt") != receipt
                or (kind == "video" and asset_metadata.get("source_sha256") != asset.sha256)
            ):
                raise ValueError("local_scene_asset_provenance_mismatch")
            if kind == "voice" and (
                asset_metadata.get("batch_sha256") != batch.sha256
                or asset.request_id != batch.request_id
                or asset.model != batch.model
            ):
                raise ValueError("local_scene_title_batch_mismatch")
    return {
        (ordinal, kind): selected[(ordinal, kind)]
        for ordinal in range(1, 13)
        for kind in ("video", "voice")
    }


def validate_render_provenance(
    value: object,
    profile: Mapping[str, Any],
    selected: Mapping[tuple[int, str], StudioAsset],
) -> dict[str, Any]:
    """Verify the fixed renderer receipt without resolving calendar data upstream."""
    if not isinstance(value, dict) or value.get("render_contract") != "scene-v2":
        raise ValueError("scene_render_provenance_missing")
    for key in (
        "render_preset_id",
        "render_preset_sha256",
        "voice_profile_sha256",
        "voice_profile",
    ):
        if value.get(key) != profile[key]:
            raise ValueError("scene_render_profile_mismatch")
    for key, kind in (("title_audio_sha256", "voice"), ("video_sha256", "video")):
        if value.get(key) != [selected[(ordinal, kind)].sha256 for ordinal in range(1, 13)]:
            raise ValueError("scene_render_inputs_mismatch")
    hashes = value.get("assembled_wav_sha256")
    clips = value.get("bank_clip_refs")
    if (
        not isinstance(hashes, list)
        or len(hashes) != 12
        or not all(_hash(item) for item in hashes)
        or not _hash(value.get("month_bank_sha256"))
        or not isinstance(clips, list)
        or len(clips) != 12
        or any(
            not isinstance(clip, dict)
            or clip.get("ordinal") != ordinal
            or not _hash(clip.get("sha256"))
            for ordinal, clip in enumerate(clips, 1)
        )
        or not isinstance(value.get("month_bank_source"), dict)
        or not _hash(value["month_bank_source"].get("audio_sha256"))
    ):
        raise ValueError("scene_render_dependencies_missing")
    return dict(value)


def _hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
