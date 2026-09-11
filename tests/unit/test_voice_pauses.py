import math
import wave
from pathlib import Path

import pytest

from myloware.studio.voice_batch import TimedWord, VoiceBatchAlignmentError, split_wav
from myloware.studio.voice_pauses import Silence, detect_silences, plan_pause_splits

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def make_wav(p: Path, gap=True):
    rate = 24000
    frames = []
    for i in range(12):
        frames += [int(12000 * math.sin(2 * math.pi * 440 * n / rate)) for n in range(rate // 2)]
        if gap and i < 11:
            frames += [0] * (rate // 2)
    with wave.open(str(p), "w") as w:
        w.setparams((1, 2, rate, len(frames), "NONE", "not compressed"))
        w.writeframes(b"".join(x.to_bytes(2, "little", signed=True) for x in frames))


def words():
    out = []
    for i, m in enumerate(MONTHS):
        base = i * 1.0
        out += [
            TimedWord(m.casefold(), base + 0.12, base + 0.25),
            TimedWord("teacup", base + 0.30, base + 0.42),
        ]
    return out


@pytest.mark.asyncio
async def test_pause_cuts_preserve_onsets_and_use_measured_silence(tmp_path):
    p = tmp_path / "source.wav"
    make_wav(p)
    silences = await detect_silences(p, 11.5)
    splits = plan_pause_splits(tuple(f"{m}. Teacup." for m in MONTHS), words(), 11.5, silences)
    assert splits[0].start_seconds == 0
    assert splits[9].start_seconds < 9.0  # October onset retained despite late word time
    for cut in [x.end_seconds for x in splits[:-1]] + [x.start_seconds for x in splits[1:]]:
        assert any(s.start_seconds <= cut <= s.end_seconds for s in silences)
    outputs = await split_wav(p, splits, tmp_path / "split")
    with wave.open(str(outputs[9]), "rb") as child:
        data = child.readframes(child.getnframes())
        values = [
            int.from_bytes(data[i : i + 2], "little", signed=True) for i in range(0, len(data), 2)
        ]
        onset = next(i for i, value in enumerate(values) if abs(value) > 100)
        assert onset / 24000 == pytest.approx(9.0 - splits[9].start_seconds, abs=0.002)
        assert sum(abs(value) > 100 for value in values) > 11500
    with wave.open(str(outputs[0]), "rb") as first, wave.open(str(p), "rb") as source:
        assert first.readframes(100) == source.readframes(100)


@pytest.mark.asyncio
async def test_missing_or_unordered_gaps_fail_closed(tmp_path):
    p = tmp_path / "no-gap.wav"
    make_wav(p, False)
    with pytest.raises(VoiceBatchAlignmentError, match="quiet gap"):
        plan_pause_splits(tuple(f"{m}. Teacup." for m in MONTHS), words(), 11.5, ())
    with pytest.raises(VoiceBatchAlignmentError):
        plan_pause_splits(
            tuple(f"{m}. Teacup." for m in MONTHS), words(), 11.5, (Silence(2, 3), Silence(1, 1.5))
        )
