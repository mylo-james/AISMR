from __future__ import annotations

import shutil

import pytest

from myloware.studio.fixtures import fixture_batch_timestamps, prepare_fixture_media
from myloware.studio.media import verify_media_file
from myloware.studio.voice_batch import (
    build_narration_text,
    build_scene_narration_text,
    canonical_narration_lines,
    normalize_fal_timestamps,
    scene_narration_lines,
)
from myloware.studio.voice_pauses import detect_silences, plan_pause_splits
from myloware.workflows.monthly import deterministic_fixture_plan
from myloware.workflows.scenes import deterministic_scene_plan


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("say") is None, reason="requires installed macOS speech")
@pytest.mark.asyncio
async def test_fixture_batch_has_explicit_synthetic_proof_and_measured_split_gaps(tmp_path) -> None:
    plan = deterministic_fixture_plan(__import__("uuid").uuid4(), "teacup")
    try:
        await prepare_fixture_media(plan, tmp_path)
    except RuntimeError as exc:
        if str(exc) == "fixture speech synthesis produced no audio":
            pytest.skip("installed macOS speech service produced no audio")
        raise
    root = tmp_path / str(plan.run_id)
    batch = await verify_media_file(root / "voice-batch.wav", kind="audio")
    timestamps = fixture_batch_timestamps(tmp_path, str(plan.run_id))
    lines = canonical_narration_lines(plan)
    words = normalize_fal_timestamps(timestamps, build_narration_text(lines))
    splits = plan_pause_splits(
        lines,
        words,
        batch.duration_seconds,
        await detect_silences(batch.path, batch.duration_seconds),
    )
    assert len(splits) == 12 and splits[0].start_seconds == 0


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("say") is None, reason="requires installed macOS speech")
@pytest.mark.asyncio
async def test_scene_fixture_batch_splits_twelve_title_only_clips(tmp_path) -> None:
    plan = deterministic_scene_plan(__import__("uuid").uuid4(), "teacup")
    try:
        await prepare_fixture_media(plan, tmp_path)
    except RuntimeError as exc:
        if str(exc) == "fixture speech synthesis produced no audio":
            pytest.skip("installed macOS speech service produced no audio")
        raise
    root = tmp_path / str(plan.run_id)
    batch = await verify_media_file(root / "title-batch.wav", kind="audio")
    timestamps = fixture_batch_timestamps(tmp_path, str(plan.run_id), scene_mode=True)
    lines = scene_narration_lines(plan)
    words = normalize_fal_timestamps(timestamps, build_scene_narration_text(lines))
    splits = plan_pause_splits(
        lines,
        words,
        batch.duration_seconds,
        await detect_silences(batch.path, batch.duration_seconds),
    )
    assert len(splits) == 12 and all(split.narration == line for split, line in zip(splits, lines))
