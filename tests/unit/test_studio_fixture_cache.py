from __future__ import annotations

import json
import wave
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from myloware.config.studio import StudioSettings
from myloware.studio import fixtures
from myloware.studio.moderation import FixtureModerator
from myloware.studio.service import StudioService
from myloware.studio.voice_batch import (
    build_narration_text,
    canonical_narration_lines,
    normalize_fal_timestamps,
)
from myloware.workflows.monthly import MonthIdea, MonthlyPlan, deterministic_fixture_plan


def _write_wav(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = content.encode() * 40
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(frames)


def _revision(plan: MonthlyPlan) -> MonthlyPlan:
    changed = {1, 5, 9}
    ideas = tuple(
        (
            MonthIdea(
                month=idea.month,
                ordinal=idea.ordinal,
                label=f"Revised fixture scene {idea.ordinal}",
                visual_prompt=idea.visual_prompt,
                spoken_text=f"{idea.month}. Revised fixture scene {idea.ordinal}.",
            )
            if idea.ordinal in changed
            else idea
        )
        for idea in plan.ideas
    )
    return MonthlyPlan.model_validate(plan.model_dump() | {"revision": 2, "ideas": ideas})


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_voice", [b"", b"truncated fixture speech"])
async def test_fixture_narration_cache_is_exact_line_bound_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, corrupt_voice: bytes
) -> None:
    calls: list[tuple[str, Path]] = []

    async def fake_command(*args: str) -> None:
        output = Path(args[args.index("-o") + 1]) if args[0] == "say" else Path(args[-1])
        calls.append((args[0], output))
        if output.suffix == ".wav":
            _write_wav(output, args[-1])
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"fixture video")

    monkeypatch.setattr(fixtures, "_command", fake_command)
    initial = deterministic_fixture_plan(uuid4(), "teacup")
    await fixtures.prepare_fixture_media(initial, tmp_path)
    directory = tmp_path / str(initial.run_id)
    videos = {
        path.name: sha256(path.read_bytes()).hexdigest() for path in directory.glob("month-*.mp4")
    }
    voices = {
        path.name: sha256(path.read_bytes()).hexdigest() for path in directory.glob("month-*.wav")
    }
    initial_say_calls = len([call for call in calls if call[0] == "say"])

    (directory / "month-02.wav").write_bytes(corrupt_voice)
    revised = _revision(initial)
    await fixtures.prepare_fixture_media(revised, tmp_path)
    revised_voices = {
        path.name: sha256(path.read_bytes()).hexdigest() for path in directory.glob("month-*.wav")
    }
    assert {
        path.name: sha256(path.read_bytes()).hexdigest() for path in directory.glob("month-*.mp4")
    } == videos
    assert len([call for call in calls if call[0] == "say"]) == initial_say_calls + 4
    assert any(output.name == ".month-02.tmp.wav" for command, output in calls if command == "say")
    assert await fixtures._duration(directory / "month-02.wav") > 0
    for name, value in voices.items():
        if name not in {"month-01.wav", "month-05.wav", "month-09.wav"}:
            assert revised_voices[name] == value
    lines = canonical_narration_lines(revised)
    timestamps = fixtures.fixture_batch_timestamps(tmp_path, str(revised.run_id))
    assert normalize_fal_timestamps(timestamps, build_narration_text(lines))

    before_idempotent = list(calls)
    await fixtures.prepare_fixture_media(revised, tmp_path)
    assert calls == before_idempotent

    sidecar = directory / "voice-batch-timestamps.json"
    corrupt = json.loads(sidecar.read_text(encoding="utf-8"))
    corrupt["timestamps"] = [{"characters": ["x"]}]
    sidecar.write_text(json.dumps(corrupt), encoding="utf-8")
    await fixtures.prepare_fixture_media(revised, tmp_path)
    assert len([call for call in calls if call[0] == "say"]) == initial_say_calls + 16
    repaired = fixtures.fixture_batch_timestamps(tmp_path, str(revised.run_id))
    assert normalize_fal_timestamps(repaired, build_narration_text(lines))

    service = StudioService(
        SimpleNamespace(
            config=StudioSettings(fixture_root=tmp_path),
            sign=lambda *_args: "fixture-test-token",
        ),
        moderator=FixtureModerator("allow"),
    )
    provider = service.assets(revised.run_id).voice_provider
    submitted = await provider.submit_batch(lines=lines, operation_key="fixture-revision")
    receipt = await provider.poll(submitted.request_id)
    assert receipt.timestamps == timestamps
    assert normalize_fal_timestamps(receipt.timestamps, build_narration_text(lines))
