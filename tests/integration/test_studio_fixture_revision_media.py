from __future__ import annotations

import shutil
from hashlib import sha256
from uuid import uuid4

import pytest

from myloware.studio.fixtures import fixture_batch_timestamps, prepare_fixture_media
from myloware.studio.media import verify_media_file
from myloware.studio.voice_batch import (
    build_narration_text,
    canonical_narration_lines,
    normalize_fal_timestamps,
)
from myloware.studio.voice_pauses import detect_silences, plan_pause_splits
from myloware.workflows.monthly import MonthIdea, MonthlyPlan, deterministic_fixture_plan


def _sha256(path) -> str:  # type: ignore[no-untyped-def]
    return sha256(path.read_bytes()).hexdigest()


def _revision(plan: MonthlyPlan) -> MonthlyPlan:
    replacements = {1, 5, 9}
    ideas = tuple(
        (
            MonthIdea(
                month=idea.month,
                ordinal=idea.ordinal,
                label=f"Revised fixture scene {idea.ordinal}",
                visual_prompt=idea.visual_prompt,
                spoken_text=f"{idea.month}. Revised fixture scene {idea.ordinal}.",
            )
            if idea.ordinal in replacements
            else idea
        )
        for idea in plan.ideas
    )
    return MonthlyPlan.model_validate(plan.model_dump() | {"revision": 2, "ideas": ideas})


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("say") is None, reason="requires installed macOS speech")
@pytest.mark.asyncio
async def test_fixture_revision_regenerates_changed_narration_and_keeps_videos(tmp_path) -> None:
    initial = deterministic_fixture_plan(uuid4(), "teacup")
    try:
        await prepare_fixture_media(initial, tmp_path)
    except RuntimeError as exc:
        if str(exc) == "fixture speech synthesis produced no audio":
            pytest.skip("installed macOS speech service produced no audio")
        raise
    directory = tmp_path / str(initial.run_id)
    initial_videos = {_path.name: _sha256(_path) for _path in directory.glob("month-*.mp4")}
    initial_voices = {_path.name: _sha256(_path) for _path in directory.glob("month-*.wav")}
    initial_batch = _sha256(directory / "voice-batch.wav")

    revised = _revision(initial)
    await prepare_fixture_media(revised, tmp_path)

    assert {_path.name: _sha256(_path) for _path in directory.glob("month-*.mp4")} == initial_videos
    revised_voices = {_path.name: _sha256(_path) for _path in directory.glob("month-*.wav")}
    assert {
        name: revised_voices[name]
        for name in initial_voices
        if name not in {"month-01.wav", "month-05.wav", "month-09.wav"}
    } == {
        name: initial_voices[name]
        for name in initial_voices
        if name not in {"month-01.wav", "month-05.wav", "month-09.wav"}
    }
    assert _sha256(directory / "voice-batch.wav") != initial_batch

    lines = canonical_narration_lines(revised)
    timestamps = fixture_batch_timestamps(tmp_path, str(revised.run_id))
    words = normalize_fal_timestamps(timestamps, build_narration_text(lines))
    batch = await verify_media_file(directory / "voice-batch.wav", kind="audio")
    splits = plan_pause_splits(
        lines,
        words,
        batch.duration_seconds,
        await detect_silences(batch.path, batch.duration_seconds),
    )
    assert len(splits) == 12

    before_idempotent = {
        path.name: _sha256(path)
        for path in [*directory.glob("month-*.wav"), directory / "voice-batch.wav"]
    }
    await prepare_fixture_media(revised, tmp_path)
    assert {
        path.name: _sha256(path)
        for path in [*directory.glob("month-*.wav"), directory / "voice-batch.wav"]
    } == before_idempotent
