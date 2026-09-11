"""Fail-closed local splitting for one selected Fal batch narration response.

The provider adapter supplies an explicit, evidence-verified word timestamp
list. This module refuses unknown response shapes, does not download audio, and
uses only local FFmpeg argument vectors for the split.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from myloware.workflows.monthly import CANONICAL_MONTHS, MonthlyPlan

_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)


class VoiceBatchAlignmentError(ValueError):
    """The timestamp payload cannot safely map one WAV to twelve narrations."""


@dataclass(frozen=True)
class TimedWord:
    text: str
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class VoiceSplit:
    ordinal: int
    narration: str
    start_seconds: float
    end_seconds: float


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(token.casefold().replace("’", "'") for token in _TOKEN.findall(text))


def canonical_narration_lines(plan: MonthlyPlan) -> tuple[str, ...]:
    """Return the twelve exact `Month. Label.` lines owned by the approved plan."""

    if len(plan.ideas) != len(CANONICAL_MONTHS):
        raise VoiceBatchAlignmentError("monthly plan must contain exactly twelve ideas")
    lines: list[str] = []
    for ordinal, (idea, month) in enumerate(zip(plan.ideas, CANONICAL_MONTHS, strict=True), 1):
        expected = f"{month}. {idea.label}."
        if idea.ordinal != ordinal or idea.month != month or idea.spoken_text != expected:
            raise VoiceBatchAlignmentError("monthly plan narration is not canonical")
        lines.append(expected)
    return tuple(lines)


def scene_narration_lines(plan: object) -> tuple[str, ...]:
    """Return the twelve exact title cues for a schema-v2 scene plan.

    This deliberately accepts the scene contract structurally.  The plan reader
    owns schema dispatch, while this media boundary only needs the approved
    ordinal/title pairs and must never infer a calendar label.
    """

    scenes = getattr(plan, "scenes", None)
    if not isinstance(scenes, tuple) or len(scenes) != len(CANONICAL_MONTHS):
        raise VoiceBatchAlignmentError("scene plan must contain exactly twelve scenes")
    lines: list[str] = []
    for ordinal, scene in enumerate(scenes, 1):
        title = getattr(scene, "title", None)
        if getattr(scene, "ordinal", None) != ordinal or not isinstance(title, str):
            raise VoiceBatchAlignmentError("scene narration is not canonical")
        _validate_title(title)
        lines.append(f"{title}.")
    return tuple(lines)


def _validate_title(title: str) -> None:
    if (
        not title
        or len(title) > 80
        or title != title.strip()
        or "\n" in title
        or not _tokens(title)
        or any(
            character in ".[]<>" or ord(character) < 32 or ord(character) == 127
            for character in title
        )
    ):
        raise VoiceBatchAlignmentError("scene title is unsafe or invalid")


def build_narration_text(lines: Sequence[str]) -> str:
    """Build one naturally delivered V3 batch with long-pause tags.

    The tag strings are delivery controls, not narration tokens.  Every canonical
    spoken token remains in order. A blank line separates cues; the final cue has
    no trailing long-pause instruction.
    """

    if len(lines) != len(CANONICAL_MONTHS):
        raise VoiceBatchAlignmentError("exactly twelve narration lines are required")
    rendered: list[str] = []
    for index, (month, line) in enumerate(zip(CANONICAL_MONTHS, lines, strict=True)):
        if not isinstance(line, str) or "\n" in line or not line.startswith(f"{month}. "):
            raise VoiceBatchAlignmentError("narration line is not canonical")
        label = line.removeprefix(f"{month}. ")
        if (
            not label.endswith(".")
            or not _tokens(label[:-1])
            or len(label[:-1]) > 80
            or any(
                character in "[]<>" or ord(character) < 32 or ord(character) == 127
                for character in label
            )
        ):
            raise VoiceBatchAlignmentError("narration label is unsafe or invalid")
        pause = " [long pause]" if index < len(CANONICAL_MONTHS) - 1 else ""
        rendered.append(f"{month}... {label}{pause}")
    return "\n\n".join(rendered)


def build_scene_narration_text(
    lines: Sequence[str], *, delivery_cue: str = "[whispers]", between_cues: str = "[long pause]"
) -> str:
    """Build the pinned title-only request text for a scene narration batch.

    ``lines`` are the durable, user-reviewed cues.  The delivery cue and pause
    markers are provider controls and are intentionally excluded from those
    lines, allowing timestamp validation to prove the exact submitted text.
    """

    if len(lines) != len(CANONICAL_MONTHS):
        raise VoiceBatchAlignmentError("exactly twelve narration lines are required")
    if delivery_cue != "[whispers]" or between_cues != "[long pause]":
        raise VoiceBatchAlignmentError("scene narration delivery cue is unsupported")
    rendered: list[str] = []
    for index, line in enumerate(lines):
        if not isinstance(line, str) or not line.endswith("."):
            raise VoiceBatchAlignmentError("scene narration line is not canonical")
        _validate_title(line[:-1])
        pause = f" {between_cues}" if index < len(lines) - 1 else ""
        rendered.append(f"{delivery_cue} {line}{pause}")
    return "\n\n".join(rendered)


def normalize_fal_timestamps(raw: object, input_text: str) -> tuple[TimedWord, ...]:
    """Normalize the observed Fal V3 character-timestamp chunk list.

    This accepts only the observed list of chunks with exactly ``characters``,
    ``character_start_times_seconds``, and ``character_end_times_seconds``.
    Chunks may be empty or cut through a word. The flattened characters must
    exactly equal the submitted cue text. Only ``[whispers]`` and ``[long pause]``
    are delivery instructions; every other bracket expression is rejected.
    """

    if not isinstance(input_text, str) or not input_text:
        raise VoiceBatchAlignmentError("submitted narration text is required")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise VoiceBatchAlignmentError("Fal timestamps must be a chunk sequence")
    characters: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    previous_end = 0.0
    epsilon = 1e-7
    for chunk in raw:
        if not isinstance(chunk, Mapping) or set(chunk) != {
            "characters",
            "character_start_times_seconds",
            "character_end_times_seconds",
        }:
            raise VoiceBatchAlignmentError("Fal timestamp chunk has an unsupported shape")
        chars = chunk["characters"]
        chunk_starts = chunk["character_start_times_seconds"]
        chunk_ends = chunk["character_end_times_seconds"]
        if (
            not isinstance(chars, Sequence)
            or isinstance(chars, (str, bytes))
            or not isinstance(chunk_starts, Sequence)
            or isinstance(chunk_starts, (str, bytes))
            or not isinstance(chunk_ends, Sequence)
            or isinstance(chunk_ends, (str, bytes))
            or len(chars) != len(chunk_starts)
            or len(chars) != len(chunk_ends)
        ):
            raise VoiceBatchAlignmentError("Fal timestamp chunk arrays must have equal lengths")
        for character, start, end in zip(chars, chunk_starts, chunk_ends, strict=True):
            if (
                not isinstance(character, str)
                or len(character) != 1
                or not isinstance(start, (int, float))
                or isinstance(start, bool)
                or not isinstance(end, (int, float))
                or isinstance(end, bool)
                or not math.isfinite(float(start))
                or not math.isfinite(float(end))
                or float(start) < -epsilon
                or float(end) + epsilon < float(start)
                or float(start) + epsilon < previous_end
            ):
                raise VoiceBatchAlignmentError(
                    "Fal character timings must be finite and globally ordered"
                )
            normalized_start = max(0.0, previous_end, float(start))
            normalized_end = max(normalized_start, float(end))
            characters.append(character)
            starts.append(normalized_start)
            ends.append(normalized_end)
            previous_end = normalized_end
    flattened = "".join(characters)
    if flattened != input_text:
        raise VoiceBatchAlignmentError(
            "Fal timestamp text does not exactly match submitted narration"
        )
    masked = list(flattened)
    known_tag = re.compile(r"\[(?:whispers|long pause)\]")
    for match in known_tag.finditer(flattened):
        masked[match.start() : match.end()] = " " * (match.end() - match.start())
    if "[" in "".join(masked) or "]" in "".join(masked):
        raise VoiceBatchAlignmentError("Fal narration contains an unknown bracket instruction")
    words: list[TimedWord] = []
    for match in _TOKEN.finditer("".join(masked)):
        token = _tokens(match.group())[0]
        words.append(TimedWord(token, starts[match.start()], ends[match.end() - 1]))
    return normalize_timestamp_words(
        [
            {"text": word.text, "start": word.start_seconds, "end": word.end_seconds}
            for word in words
        ]
    )


def normalize_timestamp_words(raw: object) -> tuple[TimedWord, ...]:
    """Validate an explicit ``[{text, start, end}, ...]`` word list.

    No undocumented response object, character alignment, missing field, token
    expansion, or multi-word timestamp is guessed. The adapter must prove and
    explicitly extract the provider's word list before calling this function.
    """

    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise VoiceBatchAlignmentError("word timestamps must be an explicit sequence")
    words: list[TimedWord] = []
    previous_end = 0.0
    for value in raw:
        if not isinstance(value, Mapping) or set(value) - {"text", "start", "end"}:
            raise VoiceBatchAlignmentError("word timestamp has an unsupported shape")
        text, start, end = value.get("text"), value.get("start"), value.get("end")
        if not isinstance(text, str) or len(_tokens(text)) != 1:
            raise VoiceBatchAlignmentError("each timestamp must name exactly one word")
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or float(start) < 0
            or float(end) <= float(start)
            or float(start) < previous_end
        ):
            raise VoiceBatchAlignmentError(
                "word timestamps must be finite, ordered, and non-overlapping"
            )
        words.append(TimedWord(_tokens(text)[0], float(start), float(end)))
        previous_end = float(end)
    if not words:
        raise VoiceBatchAlignmentError("word timestamps are empty")
    return tuple(words)


def plan_splits(
    lines: Sequence[str],
    words: Sequence[TimedWord],
    audio_duration: float,
    *,
    leading_padding_seconds: float = 0.20,
    trailing_padding_seconds: float = 0.35,
) -> tuple[VoiceSplit, ...]:
    """Map exact spoken tokens while capping retained pauses at each child boundary."""

    if len(lines) != len(CANONICAL_MONTHS):
        raise VoiceBatchAlignmentError("exactly twelve narration lines are required")
    previous_end = 0.0
    for word in words:
        normalized = _tokens(word.text) if isinstance(word, TimedWord) else ()
        if (
            not isinstance(word, TimedWord)
            or len(normalized) != 1
            or word.text != normalized[0]
            or not isinstance(word.start_seconds, (int, float))
            or isinstance(word.start_seconds, bool)
            or not isinstance(word.end_seconds, (int, float))
            or isinstance(word.end_seconds, bool)
            or not math.isfinite(float(word.start_seconds))
            or not math.isfinite(float(word.end_seconds))
            or float(word.start_seconds) < previous_end
            or float(word.end_seconds) <= float(word.start_seconds)
        ):
            raise VoiceBatchAlignmentError(
                "word timestamps must be finite, ordered, and non-overlapping"
            )
        previous_end = float(word.end_seconds)
    for padding in (leading_padding_seconds, trailing_padding_seconds):
        if (
            not isinstance(padding, (int, float))
            or isinstance(padding, bool)
            or not math.isfinite(float(padding))
            or float(padding) < 0
        ):
            raise VoiceBatchAlignmentError("split padding must be finite and non-negative")
    if (
        not isinstance(audio_duration, (int, float))
        or isinstance(audio_duration, bool)
        or not math.isfinite(float(audio_duration))
        or float(audio_duration) <= 0
    ):
        raise VoiceBatchAlignmentError("audio duration must be finite and positive")
    expected = tuple(token for line in lines for token in _tokens(line))
    observed = tuple(word.text for word in words)
    if not expected or observed != expected:
        raise VoiceBatchAlignmentError(
            "timestamp words do not exactly match the twelve narration lines"
        )
    if words[-1].end_seconds > float(audio_duration):
        raise VoiceBatchAlignmentError("word timing exceeds the local audio duration")

    spans: list[tuple[TimedWord, TimedWord]] = []
    cursor = 0
    for line in lines:
        count = len(_tokens(line))
        spans.append((words[cursor], words[cursor + count - 1]))
        cursor += count
    starts = [max(0.0, spans[0][0].start_seconds - float(leading_padding_seconds))]
    ends: list[float] = []
    for index in range(len(spans) - 1):
        current_last = spans[index][1]
        following_first = spans[index + 1][0]
        gap = following_first.start_seconds - current_last.end_seconds
        # Keep at most the requested local padding. If the gap is shorter than
        # both caps, divide only that available gap without cutting either word.
        kept_trailing = min(float(trailing_padding_seconds), gap / 2)
        kept_leading = min(float(leading_padding_seconds), gap - kept_trailing)
        ends.append(current_last.end_seconds + kept_trailing)
        starts.append(following_first.start_seconds - kept_leading)
    ends.append(
        min(float(audio_duration), spans[-1][1].end_seconds + float(trailing_padding_seconds))
    )
    splits: list[VoiceSplit] = []
    for index, (line, start, end) in enumerate(zip(lines, starts, ends, strict=True)):
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise VoiceBatchAlignmentError("computed split is invalid")
        splits.append(VoiceSplit(index + 1, line, start, end))
    return tuple(splits)


def ffmpeg_split_commands(
    source_audio: Path, splits: Sequence[VoiceSplit], output_dir: Path
) -> tuple[tuple[str, ...], ...]:
    """Build fixed ffmpeg argv tuples for local WAV extraction, with no shell."""

    if not source_audio.is_file():
        raise VoiceBatchAlignmentError("source audio must be an existing local file")
    if len(splits) != len(CANONICAL_MONTHS) or [split.ordinal for split in splits] != list(
        range(1, 13)
    ):
        raise VoiceBatchAlignmentError("exactly twelve ordered splits are required")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise VoiceBatchAlignmentError("voice output directory must be absent or empty")
    return tuple(
        (
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-ss",
            f"{split.start_seconds:.6f}",
            "-t",
            f"{split.end_seconds - split.start_seconds:.6f}",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source_audio),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(output_dir / f"{split.ordinal:02d}-voice.wav"),
        )
        for split in splits
    )


async def split_wav(
    source_audio: Path,
    splits: Sequence[VoiceSplit],
    output_dir: Path,
    *,
    runner: Callable[[tuple[str, ...]], Awaitable[None]] | None = None,
) -> tuple[Path, ...]:
    """Execute local ffmpeg argument vectors; no network or shell is involved."""

    commands = ffmpeg_split_commands(source_audio, splits, output_dir)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def default_runner(command: tuple[str, ...]) -> None:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            await asyncio.wait_for(process.communicate(), timeout=60)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise VoiceBatchAlignmentError("ffmpeg voice split timed out") from None
        if process.returncode:
            raise VoiceBatchAlignmentError("ffmpeg voice split failed") from None

    execute = runner or default_runner
    for command in commands:
        await execute(command)
    outputs = tuple(output_dir / f"{split.ordinal:02d}-voice.wav" for split in splits)
    if runner is None and not all(path.is_file() and path.stat().st_size > 0 for path in outputs):
        raise VoiceBatchAlignmentError("ffmpeg did not produce every voice WAV")
    return outputs
