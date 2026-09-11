from __future__ import annotations

import json

import httpx
import pytest

from myloware.providers.fal_transport import FalQueueClient
from myloware.providers.fal_voice_batch import (
    ELEVEN_V3_MODEL,
    FalNarrationBatchProvider,
)
from myloware.providers.media import SubmissionUnknown, SubmitRejected
from myloware.studio.execution_profile import VoiceProfile
from myloware.workflows.monthly import CANONICAL_MONTHS

LINES = tuple(f"{month}. Frozen Teacup." for month in CANONICAL_MONTHS)
TITLE_LINES = tuple(f"Scene {index}." for index in range(1, 13))


@pytest.mark.asyncio
async def test_scene_batch_uses_only_pinned_profile_and_title_cues() -> None:
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"request_id": "scene-title-batch"})

    profile = VoiceProfile(
        profile_version="eleven-v3-whisper-en-v1",
        model=ELEVEN_V3_MODEL,
        voice="KmnvDXRA0HU55Q0aqkPG",
        stability=0.5,
        language_code="en",
        apply_text_normalization="off",
        timestamps=True,
        delivery_prefix="[whispers]",
        between_cues="[long pause]",
    )
    client = FalQueueClient(key="test-key", transport=httpx.MockTransport(respond))
    provider = FalNarrationBatchProvider(client, voice="must-not-be-used")
    await provider.submit_batch(
        lines=TITLE_LINES,
        operation_key="run:2:title-batch",
        voice_profile=profile.model_dump(mode="json"),
    )
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload == {
        "text": "\n\n".join(
            f"[whispers] {line}" + (" [long pause]" if index < 11 else "")
            for index, line in enumerate(TITLE_LINES)
        ),
        "voice": profile.voice,
        "timestamps": True,
        "stability": profile.stability,
        "language_code": profile.language_code,
        "apply_text_normalization": profile.apply_text_normalization,
    }
    assert all(month not in payload["text"] for month in CANONICAL_MONTHS)
    await provider.aclose()


@pytest.mark.asyncio
async def test_one_twelve_line_natural_request_with_timestamps() -> None:
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"request_id": "one-narration-batch"})

    client = FalQueueClient(key="test-key", transport=httpx.MockTransport(respond))
    provider = FalNarrationBatchProvider(client)
    result = await provider.submit_batch(lines=LINES, operation_key="run:1:voice-batch")
    assert result.request_id == "one-narration-batch"
    assert result.model == ELEVEN_V3_MODEL
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"https://queue.fal.run/{ELEVEN_V3_MODEL}"
    assert request.headers["X-Fal-No-Retry"] == "1"
    payload = json.loads(request.content)
    assert payload["timestamps"] is True
    assert payload["voice"] == "KmnvDXRA0HU55Q0aqkPG"
    assert payload["text"].count("[whispers]") == 0
    for month in CANONICAL_MONTHS:
        assert payload["text"].count(month) == 1
    await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [(400, SubmitRejected), (429, SubmissionUnknown), (503, SubmissionUnknown)],
)
async def test_batch_submit_never_retries_uncertain_or_rejected_response(status, expected) -> None:
    calls = []

    async def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "test failure"})

    client = FalQueueClient(key="test-key", transport=httpx.MockTransport(respond))
    provider = FalNarrationBatchProvider(client)
    with pytest.raises(expected):
        await provider.submit_batch(lines=LINES, operation_key="run:1:voice-batch")
    assert len(calls) == 1
    await provider.aclose()


@pytest.mark.asyncio
async def test_batch_poll_preserves_words_and_reuses_saved_id() -> None:
    words = [{"word": "January.", "start": 0.1, "end": 0.8}]
    calls = []

    class Completed:
        pass

    class Handle:
        async def status(self, *, with_logs):
            assert with_logs is False
            return Completed()

        async def get(self):
            return {
                "audio": {"url": "https://v3.fal.media/audio.mp3"},
                "timestamps": words,
            }

    class Client:
        async def get_handle(self, application, request_id):
            calls.append((application, request_id))
            return Handle()

    result = await FalNarrationBatchProvider(Client()).poll("saved-id")
    assert calls == [(ELEVEN_V3_MODEL, "saved-id")]
    assert result.state == "ready" and result.timestamps == tuple(words)


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamps", [None, [], "bad", [1]])
async def test_missing_timestamps_never_become_usable_audio(timestamps) -> None:
    class Completed:
        pass

    class Handle:
        async def status(self, *, with_logs):
            return Completed()

        async def get(self):
            return {
                "audio": {"url": "https://v3.fal.media/audio.mp3"},
                "timestamps": timestamps,
            }

    class Client:
        async def get_handle(self, application, request_id):
            return Handle()

    result = await FalNarrationBatchProvider(Client()).poll("saved-id")
    assert result.state == "unknown" and not result.url


@pytest.mark.asyncio
async def test_transport_still_rejects_unconfigured_model() -> None:
    client = FalQueueClient(key="test-key")
    with pytest.raises(ValueError, match="unconfigured"):
        await client.submit("untrusted/model", arguments={}, headers={})
    await client.aclose()
