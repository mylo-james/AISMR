from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from myloware.storage.studio_models import StudioAsset
from myloware.studio.execution_profile import build_execution_profile
from myloware.studio.scene_media import validate_render_provenance, validate_scene_assets
from myloware.studio.voice_batch import build_scene_narration_text, scene_narration_lines
from myloware.workflows.scenes import deterministic_scene_plan


def _inputs():
    plan = deterministic_scene_plan(uuid4(), "teacup")
    profile = build_execution_profile()
    lines = scene_narration_lines(plan)
    submitted = build_scene_narration_text(
        lines, delivery_cue="[whispers]", between_cues="[long pause]"
    )
    batch = StudioAsset(
        run_id=plan.run_id,
        revision=1,
        ordinal=0,
        kind="voice_batch",
        attempt=1,
        status="ready",
        model=profile["voice_profile"]["model"],
        request_id="receipt",
        sha256="a" * 64,
        input_hash=sha256("\n".join(lines).encode()).hexdigest(),
        media_metadata={
            "voice_profile": profile["voice_profile"],
            "voice_profile_sha256": profile["voice_profile_sha256"],
            "submitted_text": submitted,
            "submitted_text_sha256": sha256(submitted.encode()).hexdigest(),
            "timestamps_verified": True,
        },
        safety={"safe": True, "media_sha256": "a" * 64},
    )
    assets = [batch]
    for scene, line in zip(plan.scenes, lines, strict=True):
        for kind, value in (("video", scene.visual_prompt), ("voice", line)):
            content_hash = sha256(f"{kind}-{scene.ordinal}".encode()).hexdigest()
            assets.append(
                StudioAsset(
                    run_id=plan.run_id,
                    revision=1,
                    ordinal=scene.ordinal,
                    kind=kind,
                    attempt=1,
                    status="ready",
                    model=batch.model,
                    request_id="receipt",
                    sha256=content_hash,
                    artifact_id=uuid4(),
                    input_hash=sha256(value.encode()).hexdigest(),
                    media_metadata={"batch_sha256": batch.sha256},
                    safety={"safe": True, "media_sha256": content_hash},
                )
            )
    return plan, profile, assets


def test_out_of_order_assets_still_map_by_approved_ordinal() -> None:
    plan, profile, assets = _inputs()
    selected = validate_scene_assets(plan, profile, list(reversed(assets)))
    assert len(selected) == 24
    assert [selected[(index, "voice")].ordinal for index in range(1, 13)] == list(range(1, 13))


@pytest.mark.parametrize("request_id", [None, "", "   "])
def test_missing_batch_receipt_cannot_reach_renderer(request_id: str | None) -> None:
    plan, profile, assets = _inputs()
    assets[0].request_id = request_id
    for asset in assets:
        if asset.kind == "voice":
            asset.request_id = request_id
    with pytest.raises(ValueError, match="scene_narration_provenance_mismatch"):
        validate_scene_assets(plan, profile, assets)


@pytest.mark.parametrize("change", ["title", "video", "batch", "profile", "safety"])
def test_stale_or_mixed_inputs_cannot_reach_renderer(change) -> None:
    plan, profile, assets = _inputs()
    if change == "title":
        assets[2].input_hash = "b" * 64
    elif change == "video":
        assets[1].revision = 2
    elif change == "batch":
        assets[2].media_metadata = {"batch_sha256": "c" * 64}
    elif change == "profile":
        assets[0].media_metadata["voice_profile_sha256"] = "d" * 64
    else:
        assets[1].safety = {"safe": False}
    with pytest.raises(ValueError):
        validate_scene_assets(plan, profile, assets)


def test_final_receipt_binds_all_inputs_and_preserves_audio_dependencies() -> None:
    plan, profile, assets = _inputs()
    selected = validate_scene_assets(plan, profile, assets)
    receipt = {
        "render_contract": "scene-v2",
        **{
            key: profile[key]
            for key in (
                "render_preset_id",
                "render_preset_sha256",
                "voice_profile",
                "voice_profile_sha256",
            )
        },
        "month_bank_sha256": "a" * 64,
        "month_bank_source": {"audio_sha256": "b" * 64},
        "assembled_wav_sha256": ["c" * 64] * 12,
        "bank_clip_refs": [{"ordinal": index, "sha256": "d" * 64} for index in range(1, 13)],
        "video_sha256": [selected[(index, "video")].sha256 for index in range(1, 13)],
        "title_audio_sha256": [selected[(index, "voice")].sha256 for index in range(1, 13)],
    }
    assert validate_render_provenance(receipt, profile, selected) == receipt
    for key in (
        "render_preset_sha256",
        "voice_profile_sha256",
        "assembled_wav_sha256",
        "title_audio_sha256",
        "month_bank_source",
    ):
        invalid = deepcopy(receipt)
        invalid.pop(key)
        with pytest.raises(ValueError):
            validate_render_provenance(invalid, profile, selected)
    swapped = deepcopy(receipt)
    swapped["title_audio_sha256"].reverse()
    with pytest.raises(ValueError, match="inputs_mismatch"):
        validate_render_provenance(swapped, profile, selected)


def _local_inputs():
    plan, profile, _ = _inputs()
    requested_lines = scene_narration_lines(plan)
    source_lines = [f"Saved {ordinal} Teacup." for ordinal in range(1, 13)]
    receipt = {
        "archive_id": "teacup-local-title-replay-v1",
        "manifest_sha256": "f" * 64,
        "local_batch_sha256": "b" * 64,
    }
    batch = StudioAsset(
        run_id=plan.run_id,
        revision=plan.revision,
        ordinal=0,
        kind="voice_batch",
        attempt=1,
        status="ready",
        model="local-saved-scene-v1",
        request_id="local-scene-title-batch-1234567890abcdef",
        sha256="b" * 64,
        input_hash=sha256("\n".join(requested_lines).encode()).hexdigest(),
        media_metadata={
            "alignment_status": "ordinal_only_mismatch",
            "requested_title_lines": list(requested_lines),
            "requested_title_lines_sha256": sha256("\n".join(requested_lines).encode()).hexdigest(),
            "source_title_lines": source_lines,
            "source_title_text_sha256": sha256("\n\n".join(source_lines).encode()).hexdigest(),
            "local_media_receipt": receipt,
            "voice_profile": profile["voice_profile"],
            "voice_profile_sha256": profile["voice_profile_sha256"],
            "timestamps_verified": True,
            "source_batch_sha256": "b" * 64,
        },
        safety={"safe": True, "media_sha256": "b" * 64},
    )
    assets = [batch]
    for scene, requested_line, source_line in zip(
        plan.scenes, requested_lines, source_lines, strict=True
    ):
        video_hash = sha256(f"local-video-{scene.ordinal}".encode()).hexdigest()
        voice_hash = sha256(f"local-voice-{scene.ordinal}".encode()).hexdigest()
        assets.extend(
            (
                StudioAsset(
                    run_id=plan.run_id,
                    revision=plan.revision,
                    ordinal=scene.ordinal,
                    kind="video",
                    attempt=1,
                    status="ready",
                    model="local-saved-scene-v1",
                    request_id=f"local-scene-video-{scene.ordinal:02d}-1234567890abcdef",
                    sha256=video_hash,
                    artifact_id=uuid4(),
                    input_hash=sha256(scene.visual_prompt.encode()).hexdigest(),
                    media_metadata={
                        "alignment_status": "ordinal_only_mismatch",
                        "source_title": source_line,
                        "source_title_sha256": sha256(source_line.encode()).hexdigest(),
                        "source_sha256": video_hash,
                        "local_media_receipt": receipt,
                    },
                    safety={"safe": True, "media_sha256": video_hash},
                ),
                StudioAsset(
                    run_id=plan.run_id,
                    revision=plan.revision,
                    ordinal=scene.ordinal,
                    kind="voice",
                    attempt=1,
                    status="ready",
                    model="local-saved-scene-v1",
                    request_id=batch.request_id,
                    sha256=voice_hash,
                    artifact_id=uuid4(),
                    input_hash=sha256(requested_line.encode()).hexdigest(),
                    media_metadata={
                        "alignment_status": "ordinal_only_mismatch",
                        "source_title": source_line,
                        "source_title_sha256": sha256(source_line.encode()).hexdigest(),
                        "batch_sha256": batch.sha256,
                        "local_media_receipt": receipt,
                    },
                    safety={"safe": True, "media_sha256": voice_hash},
                ),
            )
        )
    return plan, profile, assets


def test_local_scene_replay_keeps_pinned_profile_and_batch_lineage() -> None:
    plan, profile, assets = _local_inputs()
    selected = validate_scene_assets(plan, profile, assets, mode="local")
    assert len(selected) == 24


@pytest.mark.parametrize(
    "change",
    ["profile", "receipt", "batch_sha", "video_source_sha", "timestamps", "source_title"],
)
def test_local_scene_replay_rejects_tampered_archive_provenance(change: str) -> None:
    plan, profile, assets = _local_inputs()
    batch = assets[0]
    if change == "profile":
        batch.media_metadata["voice_profile_sha256"] = "e" * 64
    elif change == "receipt":
        assets[1].media_metadata["local_media_receipt"] = {"manifest_sha256": "e" * 64}
    elif change == "batch_sha":
        batch.media_metadata["source_batch_sha256"] = "e" * 64
    elif change == "video_source_sha":
        assets[1].media_metadata["source_sha256"] = "e" * 64
    elif change == "timestamps":
        batch.media_metadata["timestamps_verified"] = False
    else:
        assets[1].media_metadata["source_title"] = "Different saved title."
    with pytest.raises(ValueError):
        validate_scene_assets(plan, profile, assets, mode="local")
