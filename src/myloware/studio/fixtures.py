"""Explicit local test footage and operating-system narration for a real edit."""

import asyncio
import json
import re
import shutil
import wave
from hashlib import sha256
from pathlib import Path

from myloware.studio.voice_batch import (
    VoiceBatchAlignmentError,
    build_narration_text,
    build_scene_narration_text,
    canonical_narration_lines,
    normalize_fal_timestamps,
    scene_narration_lines,
)
from myloware.workflows.scenes import ScenePlan, StudioPlan


async def _command(*args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, _error = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("fixture command timed out") from None
    if process.returncode:
        raise RuntimeError("fixture command failed")


async def prepare_fixture_media(plan: StudioPlan, root: Path) -> None:
    """Create reproducible synthetic clips, never label them as AI generations."""
    directory = root / str(plan.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    if not shutil.which("ffmpeg"):
        raise RuntimeError("local fixture generation requires FFmpeg")
    colors = (
        "9fb9cc",
        "c99fba",
        "9fbcaa",
        "beb18f",
        "aaa6cd",
        "b39dbe",
        "89a9c0",
        "a8af8e",
        "c1a28d",
        "c79781",
        "9297b5",
        "b7c2b8",
    )
    limiter = asyncio.Semaphore(2)
    scene_mode = isinstance(plan, ScenePlan)
    lines = scene_narration_lines(plan) if scene_mode else canonical_narration_lines(plan)
    batch = directory / ("title-batch.wav" if scene_mode else "voice-batch.wav")
    sidecar = directory / (
        "title-batch-timestamps.json" if scene_mode else "voice-batch-timestamps.json"
    )
    build_text = build_scene_narration_text if scene_mode else build_narration_text
    cached_lines = _cached_narration_lines(sidecar, batch, build_text)
    changed_voices: set[int] = set()
    for index, line in enumerate(lines):
        voice = directory / _fixture_voice_name(index + 1, scene_mode)
        if (
            cached_lines is None
            or cached_lines[index] != line
            or not voice.is_file()
            or not await _usable_voice(voice)
        ):
            changed_voices.add(index)

    async def month(index: int) -> None:
        async with limiter:
            video = directory / _fixture_video_name(index + 1, scene_mode)
            voice = directory / _fixture_voice_name(index + 1, scene_mode)
            if not video.is_file():
                await _command(
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c=0x{colors[index]}:s=480x854:r=24:d={'6.734' if scene_mode else '3.375'}",
                    "-vf",
                    "drawbox=x=100:y=260:w=280:h=280:color=white@0.4:t=fill",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    str(video),
                )
            if index in changed_voices:
                # ``say`` chooses its output container from the suffix, so the
                # temporary file must remain a WAV while it protects the old cache.
                temporary_voice = voice.with_name(f".{voice.stem}.tmp.wav")
                try:
                    await _command(
                        "say",
                        "-v",
                        "Samantha",
                        "-o",
                        str(temporary_voice),
                        "--data-format=LEI16@24000",
                        lines[index],
                    )
                    if await _duration(temporary_voice) <= 0:
                        raise RuntimeError("fixture speech synthesis produced no audio")
                    temporary_voice.replace(voice)
                finally:
                    temporary_voice.unlink(missing_ok=True)

    await asyncio.gather(*(month(index) for index in range(12)))
    if changed_voices or not batch.is_file() or cached_lines != lines:
        voices = [directory / _fixture_voice_name(index, scene_mode) for index in range(1, 13)]
        durations = [await _duration(path) for path in voices]
        inputs: list[str] = []
        for index, voice in enumerate(voices):
            inputs.extend(("-i", str(voice)))
            if index < 11:
                inputs.extend(("-f", "lavfi", "-t", "1", "-i", "anullsrc=r=24000:cl=mono"))
        labels = "".join(f"[{index}:a]" for index in range(23))
        temporary_batch = batch.with_name(".voice-batch.tmp.wav")
        temporary_sidecar = sidecar.with_name(".voice-batch-timestamps.tmp.json")
        try:
            await _command(
                "ffmpeg",
                "-v",
                "error",
                "-y",
                *inputs,
                "-filter_complex",
                f"{labels}concat=n=23:v=0:a=1[out]",
                "-map",
                "[out]",
                str(temporary_batch),
            )
            temporary_sidecar.write_text(
                json.dumps(
                    {
                        "narration_lines": list(lines),
                        "narration_sha256": _narration_sha256(lines),
                        "batch_sha256": _file_sha256(temporary_batch),
                        "timestamps": _fixture_timestamps(lines, durations, build_text),
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary_batch.replace(batch)
            temporary_sidecar.replace(sidecar)
        finally:
            temporary_batch.unlink(missing_ok=True)
            temporary_sidecar.unlink(missing_ok=True)


async def _duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnframes() / handle.getframerate()
    except wave.Error:
        pass
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", stderr.decode(errors="replace"))
    if match is None:
        raise RuntimeError("fixture duration probe returned no duration")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


async def _usable_voice(path: Path) -> bool:
    try:
        return await _duration(path) > 0
    except (EOFError, OSError, RuntimeError):
        return False


def fixture_batch_timestamps(
    root: Path, run_id: str, *, scene_mode: bool = False
) -> tuple[dict[str, object], ...]:
    """Read explicit synthetic timing only for the fixture batch provider."""
    filename = "title-batch-timestamps.json" if scene_mode else "voice-batch-timestamps.json"
    data = json.loads((root / run_id / filename).read_text(encoding="utf-8"))
    timestamps = data
    if isinstance(data, dict):
        timestamps = data.get("timestamps")
    if not isinstance(timestamps, list) or not all(isinstance(value, dict) for value in timestamps):
        raise RuntimeError("fixture batch timestamp sidecar is invalid")
    return tuple(timestamps)


def _cached_narration_lines(
    sidecar: Path, batch: Path, build_text=build_narration_text
) -> tuple[str, ...] | None:
    """Return the exact lines that produced a current-format fixture sidecar."""
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    lines = data.get("narration_lines")
    digest = data.get("narration_sha256")
    batch_digest = data.get("batch_sha256")
    timestamps = data.get("timestamps")
    if (
        not isinstance(lines, list)
        or not all(isinstance(line, str) for line in lines)
        or len(lines) != 12
        or not isinstance(digest, str)
        or not isinstance(batch_digest, str)
        or not isinstance(timestamps, list)
        or not all(isinstance(timestamp, dict) for timestamp in timestamps)
    ):
        return None
    result = tuple(lines)
    if digest != _narration_sha256(result) or not batch.is_file():
        return None
    if batch_digest != _file_sha256(batch):
        return None
    try:
        normalize_fal_timestamps(timestamps, build_text(result))
    except VoiceBatchAlignmentError:
        return None
    return result


def _narration_sha256(lines: tuple[str, ...]) -> str:
    return sha256(json.dumps(lines, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _fixture_timestamps(
    lines: tuple[str, ...], durations: list[float], build_text=build_narration_text
) -> list[dict[str, object]]:
    """Synthetic character timing over known local clips and one-second gaps."""
    text = build_text(lines)
    result: list[dict[str, object]] = []
    cursor = 0
    position = 0.0
    for index, (line, duration) in enumerate(zip(lines, durations, strict=True)):
        if build_text is build_scene_narration_text:
            spoken = f"[whispers] {line}"
        else:
            spoken = line.replace(". ", "... ", 1)
        if not text.startswith(spoken, cursor):
            raise ValueError("Fixture narration does not match its spoken lines")
        end = cursor + len(spoken)
        next_start = (
            text.index(
                (
                    f"[whispers] {lines[index + 1]}"
                    if build_text is build_scene_narration_text
                    else lines[index + 1].replace(". ", "... ", 1)
                ),
                end,
            )
            if index < 11
            else end
        )
        chunks = [(text[cursor:end], position, duration)]
        if next_start > end:
            chunks.append((text[end:next_start], position + duration, 1.0))
        for characters, _start_time, span in chunks:
            starts: list[float] = []
            ends: list[float] = []
            for _character in characters:
                start_time = position
                position += span / len(characters)
                starts.append(start_time)
                ends.append(position)
            result.append(
                {
                    "characters": list(characters),
                    "character_start_times_seconds": starts,
                    "character_end_times_seconds": ends,
                }
            )
        cursor = next_start
    return result


def _fixture_video_name(ordinal: int, scene_mode: bool) -> str:
    return f"scene-{ordinal:02d}.mp4" if scene_mode else f"month-{ordinal:02d}.mp4"


def _fixture_voice_name(ordinal: int, scene_mode: bool) -> str:
    return f"scene-{ordinal:02d}-title.wav" if scene_mode else f"month-{ordinal:02d}.wav"
