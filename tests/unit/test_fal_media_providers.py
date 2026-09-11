"""Fast contract tests for the bounded fal media provider adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from myloware.providers.fal_video import WAN_FAST_MODEL, FalVideoProvider
from myloware.providers.fal_voice import KOKORO_MODEL, FalVoiceProvider
from myloware.providers.media import (
    FakeVideoProvider,
    FakeVoiceProvider,
    SubmissionUnknown,
    SubmitRejected,
)


@dataclass
class Queued:
    position: int


@dataclass
class InProgress:
    logs: list[dict[str, str]] | None = None


@dataclass
class Completed:
    error: str | None = None
    error_type: str | None = None


class FakeHandle:
    def __init__(self, request_id: str, status: object, result: object | None = None) -> None:
        self.request_id = request_id
        self._status = status
        self._result = (
            result if result is not None else {"video": {"url": "https://fal.media/a.mp4"}}
        )
        self.cancelled = False

    async def status(self, *, with_logs: bool) -> object:
        assert with_logs is False
        return self._status

    async def get(self) -> object:
        return self._result

    async def cancel(self) -> None:
        self.cancelled = True


class FakeClient:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, dict[str, object], dict[str, str]]] = []
        self.handles: dict[str, FakeHandle] = {}
        self.submit_error: BaseException | None = None

    async def submit(
        self, model: str, *, arguments: dict[str, object], headers: dict[str, str]
    ) -> FakeHandle:
        if self.submit_error:
            raise self.submit_error
        self.submissions.append((model, arguments, headers))
        request_id = f"request-{len(self.submissions)}"
        handle = FakeHandle(request_id, Queued(position=3))
        self.handles[request_id] = handle
        return handle

    async def get_handle(self, model: str, request_id: str) -> FakeHandle:
        assert model in {WAN_FAST_MODEL, KOKORO_MODEL}
        return self.handles[request_id]


class HttpError(Exception):
    def __init__(self, status_code: int, text: str = "bad request") -> None:
        super().__init__(text)
        self.status_code = status_code


@pytest.mark.asyncio
async def test_video_submits_twelve_without_waiting_and_uses_fixed_cheap_params() -> None:
    client = FakeClient()
    provider = FalVideoProvider(client)
    gate = asyncio.Event()

    async def one(ordinal: int):
        await gate.wait()
        return await provider.submit(
            prompt=f"item {ordinal}", ordinal=ordinal, operation_key=f"key-{ordinal}"
        )

    tasks = [asyncio.create_task(one(index)) for index in range(1, 13)]
    await asyncio.sleep(0)
    assert client.submissions == []
    gate.set()
    submitted = await asyncio.gather(*tasks)

    assert [entry.request_id for entry in submitted] == [
        f"request-{index}" for index in range(1, 13)
    ]
    assert len(client.submissions) == 12
    model, arguments, headers = client.submissions[0]
    assert model == WAN_FAST_MODEL
    assert arguments == {
        "prompt": "item 1",
        "num_frames": 161,
        "frames_per_second": 24,
        "resolution": "480p",
        "aspect_ratio": "9:16",
        "enable_safety_checker": True,
        "enable_output_safety_checker": True,
        "interpolator_model": "none",
        "num_interpolated_frames": 0,
        "adjust_fps_for_interpolation": False,
    }
    assert headers == {"X-Fal-No-Retry": "1"}


@pytest.mark.asyncio
async def test_video_poll_normalizes_queue_running_ready_failed_unknown_and_malformed_result() -> (
    None
):
    client = FakeClient()
    provider = FalVideoProvider(client)
    client.handles.update(
        {
            "q": FakeHandle("q", Queued(4)),
            "r": FakeHandle("r", InProgress()),
            "ok": FakeHandle("ok", Completed(), {"video": {"url": "https://fal.media/ok.mp4"}}),
            "failed": FakeHandle("failed", Completed(error="unsafe output")),
            "bad": FakeHandle("bad", Completed(), {"video": {}}),
        }
    )
    assert (await provider.poll("q")).state == "queued"
    assert (await provider.poll("q")).queue_position == 4
    assert (await provider.poll("r")).state == "running"
    assert (await provider.poll("ok")).url == "https://fal.media/ok.mp4"
    assert (await provider.poll("failed")).state == "failed"
    assert (await provider.poll("bad")).state == "unknown"


@pytest.mark.asyncio
async def test_submission_errors_distinguish_definitive_rejection_from_unknown_timeout() -> None:
    client = FakeClient()
    provider = FalVideoProvider(client)
    client.submit_error = HttpError(400, "prompt invalid")
    with pytest.raises(SubmitRejected, match="Provider HTTP 400"):
        await provider.submit(prompt="x", ordinal=1, operation_key="one")

    class TimeoutWithId(Exception):
        request_id = "possibly-accepted"

    client.submit_error = TimeoutWithId("network timed out")
    with pytest.raises(SubmissionUnknown) as raised:
        await provider.submit(prompt="x", ordinal=1, operation_key="one")
    assert raised.value.request_id == "possibly-accepted"


@pytest.mark.asyncio
async def test_restart_poll_and_cancel_use_persisted_request_id() -> None:
    client = FakeClient()
    client.handles["persisted"] = FakeHandle(
        "persisted", Completed(), {"video": {"url": "https://fal.media/x.mp4"}}
    )
    provider = FalVideoProvider(client)
    assert (await provider.poll("persisted")).state == "ready"
    await provider.cancel("persisted")
    assert client.handles["persisted"].cancelled is True


@pytest.mark.asyncio
async def test_voice_uses_fixed_voice_speed_and_accepts_audio_url() -> None:
    client = FakeClient()
    provider = FalVoiceProvider(client, voice="af_bella", speed=0.9)
    submitted = await provider.submit(prompt="January. Pool.", ordinal=1, operation_key="voice-one")
    assert submitted.model == KOKORO_MODEL
    model, arguments, headers = client.submissions[0]
    assert model == KOKORO_MODEL
    assert arguments == {"prompt": "January. Pool.", "voice": "af_bella", "speed": 0.9}
    assert headers == {"X-Fal-No-Retry": "1"}
    client.handles[submitted.request_id] = FakeHandle(
        submitted.request_id, Completed(), {"audio_url": "https://fal.media/one.wav"}
    )
    assert (await provider.poll(submitted.request_id)).url == "https://fal.media/one.wav"


@pytest.mark.asyncio
async def test_fake_providers_are_restartable_and_return_trusted_fixture_urls() -> None:
    video = FakeVideoProvider(trusted_fixture_base_url="http://127.0.0.1:8123")
    voice = FakeVoiceProvider(trusted_fixture_base_url="http://127.0.0.1:8123")
    video_request = await video.submit(prompt="one", ordinal=1, operation_key="stable")
    assert (
        video_request.request_id
        == (await video.submit(prompt="one", ordinal=1, operation_key="stable")).request_id
    )
    assert (await video.poll(video_request.request_id)).url == "http://127.0.0.1:8123/month-01.mp4"
    voice_request = await voice.submit(prompt="January. One.", ordinal=12, operation_key="voice")
    assert (await voice.poll(voice_request.request_id)).url == "http://127.0.0.1:8123/month-12.wav"


@pytest.mark.parametrize("speed", [0.1, 1.3, float("nan"), float("inf"), True])
def test_voice_speed_is_a_bounded_server_profile(speed) -> None:
    with pytest.raises(ValueError, match="between 0.8 and 1.25"):
        FalVoiceProvider(FakeClient(), speed=speed)
