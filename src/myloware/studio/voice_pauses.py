"""Use measured quiet gaps as cuts; provider text timings only locate each line."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from myloware.studio.voice_batch import (
    TimedWord,
    VoiceBatchAlignmentError,
    VoiceSplit,
    _tokens,
    plan_splits,
)


@dataclass(frozen=True)
class Silence:
    start_seconds: float
    end_seconds: float


def parse_silence_log(log: str, audio_duration: float) -> tuple[Silence, ...]:
    """Parse only the documented silencedetect interval markers, in order."""
    if not math.isfinite(audio_duration) or audio_duration <= 0:
        raise VoiceBatchAlignmentError("audio duration must be positive and finite")
    intervals: list[Silence] = []
    start: float | None = None
    for kind, raw in re.findall(r"silence_(start|end):\s*([0-9.eE+-]+)", log):
        value = float(raw)
        if not math.isfinite(value) or value < 0 or value > audio_duration + 0.01:
            raise VoiceBatchAlignmentError("silence timing exceeds the local audio")
        value = min(value, audio_duration)
        if kind == "start":
            if start is not None:
                raise VoiceBatchAlignmentError("silence intervals are malformed")
            start = value
        else:
            if start is None or value <= start:
                raise VoiceBatchAlignmentError("silence intervals are malformed")
            intervals.append(Silence(start, value))
            start = None
    if start is not None and start < audio_duration:
        intervals.append(Silence(start, audio_duration))
    return tuple(intervals)


async def detect_silences(source: Path, audio_duration: float) -> tuple[Silence, ...]:
    """Measure quiet intervals locally with one FFmpeg worker and no downloads."""
    if not source.is_file():
        raise VoiceBatchAlignmentError("source audio must be an existing local file")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-threads",
        "1",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
        "-af",
        "silencedetect=noise=-50dB:d=0.25",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise VoiceBatchAlignmentError("local silence detection failed")
    return parse_silence_log(stderr.decode("utf-8", errors="replace"), audio_duration)


def plan_pause_splits(
    lines: Sequence[str],
    words: Sequence[TimedWord],
    audio_duration: float,
    silences: Sequence[Silence],
    *,
    padding_seconds: float = 0.30,
) -> tuple[VoiceSplit, ...]:
    """Retain a little quiet audio on both sides of every complete utterance.

    Text timings establish order and approximate locations only. Each midpoint
    between adjacent lines must land in exactly one measured quiet gap. No gap
    means human review, never a fallback cut through provider word timestamps.
    Preserve the full first and last utterances, including uncertain tag-aligned
    sound. A timestamp assigned to a delivery tag does not identify audible words.
    """
    plan_splits(lines, words, audio_duration)  # Validate exact text and finite ordering.
    if (
        isinstance(padding_seconds, bool)
        or not math.isfinite(padding_seconds)
        or padding_seconds < 0.05
    ):
        raise VoiceBatchAlignmentError("quiet padding must be finite and at least 50 ms")
    previous_end = 0.0
    for pause in silences:
        if (
            not isinstance(pause, Silence)
            or not math.isfinite(pause.start_seconds)
            or not math.isfinite(pause.end_seconds)
            or pause.start_seconds < previous_end
            or pause.end_seconds <= pause.start_seconds
            or pause.end_seconds > audio_duration
        ):
            raise VoiceBatchAlignmentError("quiet intervals must be finite and ordered")
        previous_end = pause.end_seconds
    spans: list[tuple[TimedWord, TimedWord]] = []
    cursor = 0
    for line in lines:
        count = len(_tokens(line))
        spans.append((words[cursor], words[cursor + count - 1]))
        cursor += count
    starts = [0.0]
    ends: list[float] = []
    used: set[Silence] = set()
    for left, right in pairwise(spans):
        anchor = (left[1].end_seconds + right[0].start_seconds) / 2
        matches = [pause for pause in silences if pause.start_seconds < anchor < pause.end_seconds]
        if len(matches) != 1 or matches[0] in used:
            raise VoiceBatchAlignmentError(
                "no unique quiet gap between narration lines; review required"
            )
        pause = matches[0]
        used.add(pause)
        # The pause must also lie between the interiors of neighboring lines.
        if pause.start_seconds < left[0].start_seconds or pause.end_seconds > right[1].end_seconds:
            raise VoiceBatchAlignmentError("quiet gap cannot separate the expected lines")
        padding = min(padding_seconds, (pause.end_seconds - pause.start_seconds) / 2)
        ends.append(pause.start_seconds + padding)
        starts.append(pause.end_seconds - padding)
    ends.append(audio_duration)
    if any(end <= start for start, end in zip(starts, ends, strict=True)):
        raise VoiceBatchAlignmentError("quiet gaps produce an invalid narration split")
    return tuple(
        VoiceSplit(index, line, start, end)
        for index, (line, start, end) in enumerate(zip(lines, starts, ends, strict=True), 1)
    )
