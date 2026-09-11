from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from myloware.studio.voice_batch import (
    VoiceBatchAlignmentError,
    build_narration_text,
    build_scene_narration_text,
    canonical_narration_lines,
    ffmpeg_split_commands,
    normalize_fal_timestamps,
    normalize_timestamp_words,
    plan_splits,
    scene_narration_lines,
    split_wav,
)
from myloware.workflows.monthly import CANONICAL_MONTHS


def lines() -> tuple[str, ...]:
    return tuple(f"{month}. Glass Teacup." for month in CANONICAL_MONTHS)


def words_for(lines_: tuple[str, ...]) -> list[dict[str, object]]:
    result = []
    cursor = 0.0
    for token in (token for line in lines_ for token in line.replace(".", "").split()):
        result.append({"text": token, "start": cursor, "end": cursor + 0.1})
        cursor += 0.15
    return result


def test_canonical_plan_and_natural_batch_text() -> None:
    ideas = [
        SimpleNamespace(
            ordinal=index, month=month, label="Glass Teacup", spoken_text=f"{month}. Glass Teacup."
        )
        for index, month in enumerate(CANONICAL_MONTHS, 1)
    ]
    plan = SimpleNamespace(ideas=ideas)
    assert canonical_narration_lines(plan) == lines()
    batch = build_narration_text(lines())
    assert batch.count("[whispers]") == 0
    assert batch.splitlines()[0] == "January... Glass Teacup. [long pause]"
    assert batch.splitlines()[-1] == "December... Glass Teacup."
    assert "\n\n" in batch


def test_scene_title_lines_never_reintroduce_calendar_text() -> None:
    scenes = [
        SimpleNamespace(ordinal=index, title=f"Shifting Teacup {index}") for index in range(1, 13)
    ]
    plan = SimpleNamespace(scenes=tuple(scenes))
    title_lines = scene_narration_lines(plan)
    assert title_lines == tuple(f"Shifting Teacup {index}." for index in range(1, 13))
    batch = build_scene_narration_text(title_lines)
    assert batch.count("[whispers]") == 12
    assert batch.count("[long pause]") == 11
    assert all(month not in batch for month in CANONICAL_MONTHS)
    with pytest.raises(VoiceBatchAlignmentError, match="unsafe"):
        build_scene_narration_text(("[January].", *title_lines[1:]))


def test_exact_tokens_midpoint_pauses_and_local_argv(tmp_path) -> None:
    source = tmp_path / "batch.wav"
    source.write_bytes(b"wav")
    timed = normalize_timestamp_words(words_for(lines()))
    splits = plan_splits(lines(), timed, timed[-1].end_seconds + 0.2)
    assert len(splits) == 12
    assert splits[0].start_seconds == 0.0
    assert splits[0].end_seconds == pytest.approx(timed[2].end_seconds + 0.025)
    assert splits[1].start_seconds == pytest.approx(timed[3].start_seconds - 0.025)
    commands = ffmpeg_split_commands(source, splits, tmp_path / "voices")
    assert len(commands) == 12 and commands[0][0] == "ffmpeg" and "-nostdin" in commands[0]
    assert (
        "-n" in commands[0]
        and commands[0][commands[0].index("-protocol_whitelist") + 1] == "file,pipe"
    )
    assert commands[0][-1].endswith("01-voice.wav") and all(
        "shell" not in value for value in commands[0]
    )


@pytest.mark.asyncio
async def test_split_uses_injected_local_runner_without_network(tmp_path) -> None:
    source = tmp_path / "batch.wav"
    source.write_bytes(b"wav")
    timed = normalize_timestamp_words(words_for(lines()))
    splits = plan_splits(lines(), timed, timed[-1].end_seconds + 0.2)
    seen: list[tuple[str, ...]] = []

    async def fake_runner(command: tuple[str, ...]) -> None:
        seen.append(command)

    paths = await split_wav(source, splits, tmp_path / "voices", runner=fake_runner)
    assert len(seen) == len(paths) == 12 and paths[-1].name == "12-voice.wav"


@pytest.mark.parametrize(
    "bad",
    [
        [],
        [{"text": "January", "start": 0, "end": float("inf")}],
        [{"text": "January", "start": 0, "end": 0}],
        [{"text": "January", "start": 1, "end": 2}, {"text": "Glass", "start": 1.5, "end": 3}],
        [{"word": "January", "start": 0, "end": 1}],
        [{"text": "January Glass", "start": 0, "end": 1}],
    ],
)
def test_malformed_word_timings_fail_closed(bad) -> None:
    with pytest.raises(VoiceBatchAlignmentError):
        normalize_timestamp_words(bad)


def test_missing_extra_or_reordered_spoken_words_fail_closed() -> None:
    timed = normalize_timestamp_words(words_for(lines()))
    with pytest.raises(VoiceBatchAlignmentError, match="exactly match"):
        plan_splits(lines(), timed[:-1], 20)
    reordered = list(timed)
    reordered[0], reordered[1] = reordered[1], reordered[0]
    with pytest.raises(VoiceBatchAlignmentError):
        plan_splits(lines(), reordered, 20)
    with pytest.raises(VoiceBatchAlignmentError, match="exceeds"):
        plan_splits(lines(), timed, timed[-1].end_seconds - 0.01)


def test_long_model_pause_is_capped_without_accelerating_words() -> None:
    source_lines = lines()
    timed = normalize_timestamp_words(words_for(source_lines))
    words = list(timed)
    # Simulate a 10-second source pause after the first narration.
    for index in range(3, len(words)):
        word = words[index]
        words[index] = type(word)(word.text, word.start_seconds + 10, word.end_seconds + 10)
    splits = plan_splits(
        source_lines,
        words,
        words[-1].end_seconds + 1,
        leading_padding_seconds=0.2,
        trailing_padding_seconds=0.35,
    )
    assert splits[0].end_seconds == pytest.approx(words[2].end_seconds + 0.35)
    assert splits[1].start_seconds == pytest.approx(words[3].start_seconds - 0.2)
    assert splits[0].end_seconds - splits[0].start_seconds < 1
    assert splits[1].end_seconds - splits[1].start_seconds < 1


def test_direct_timedword_validation_and_markup_label_rejection() -> None:
    timed = normalize_timestamp_words(words_for(lines()))
    malformed = list(timed)
    malformed[2] = type(timed[2])(timed[2].text, float("nan"), timed[2].end_seconds)
    with pytest.raises(VoiceBatchAlignmentError, match="finite"):
        plan_splits(lines(), malformed, 20)
    unsafe = list(lines())
    unsafe[0] = "January. [long pause]."
    with pytest.raises(VoiceBatchAlignmentError, match="unsafe"):
        build_narration_text(unsafe)


def test_observed_fal_chunks_normalize_exactly_and_reject_mutations() -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "voice_batch_fal_timestamps.json").read_text()
    )
    words = normalize_fal_timestamps(fixture["timestamps"], fixture["input_text"])
    assert len(words) == 36 and words[0].text == "january" and words[-1].text == "teacup"
    assert all(word.start_seconds <= word.end_seconds for word in words)

    mismatched = fixture["input_text"] + "!"
    with pytest.raises(VoiceBatchAlignmentError, match="exactly match"):
        normalize_fal_timestamps(fixture["timestamps"], mismatched)
    malformed = json.loads(json.dumps(fixture["timestamps"]))
    malformed[0]["characters"][0] = "ab"
    with pytest.raises(VoiceBatchAlignmentError, match="finite and globally"):
        normalize_fal_timestamps(malformed, fixture["input_text"])
    unknown_tag = json.loads(json.dumps(fixture["timestamps"]))
    # Preserve exact text matching while replacing an equal-length known tag.
    text = fixture["input_text"].replace("[whispers]", "[unknown ]", 1)
    cursor = 0
    for chunk in unknown_tag:
        count = len(chunk["characters"])
        chunk["characters"] = list(text[cursor : cursor + count])
        cursor += count
    with pytest.raises(VoiceBatchAlignmentError, match="unknown bracket"):
        normalize_fal_timestamps(unknown_tag, text)
