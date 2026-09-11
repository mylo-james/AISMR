"""fal Kokoro speech adapter."""

from __future__ import annotations

from myloware.providers.fal_video import (
    FalVideoProvider,
    _brief_error,
    _is_definitive_rejection,
    _status_name,
)
from myloware.providers.media import (
    AssetResult,
    ProviderUnavailable,
    Submission,
    SubmissionUnknown,
    SubmitRejected,
)

KOKORO_MODEL = "fal-ai/kokoro/american-english"


class FalVoiceProvider(FalVideoProvider):
    """Async Kokoro adapter with one configured voice and speed for a batch."""

    def __init__(
        self,
        client: object | None = None,
        *,
        key: str | None = None,
        voice: str = "af_heart",
        speed: float = 1.0,
    ) -> None:
        super().__init__(client, key=key)
        if not isinstance(voice, str) or not voice.strip():
            raise ValueError("voice is required")
        if (
            not isinstance(speed, (int, float))
            or isinstance(speed, bool)
            or not 0.8 <= speed <= 1.25
        ):
            raise ValueError("speed must be between 0.8 and 1.25")
        self.voice = voice.strip()
        self.speed = float(speed)

    async def submit(self, *, prompt: str, ordinal: int, operation_key: str) -> Submission:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 1 <= ordinal <= 12:
            raise ValueError("ordinal must be an integer from 1 through 12")
        if not isinstance(operation_key, str) or not operation_key.strip():
            raise ValueError("operation_key is required")
        try:
            client = await self._client_or_raise()
            handle = await client.submit(
                KOKORO_MODEL,
                arguments={
                    "prompt": prompt.strip(),
                    "voice": self.voice,
                    "speed": self.speed,
                },
                headers={"X-Fal-No-Retry": "1"},
            )
            request_id = getattr(handle, "request_id", None)
            if not isinstance(request_id, str) or not request_id:
                raise SubmissionUnknown("Fal accepted an invalid submission receipt")
            return Submission(request_id=request_id, model=KOKORO_MODEL)
        except (ProviderUnavailable, SubmissionUnknown, ValueError):
            raise
        except Exception as exc:
            if _is_definitive_rejection(exc):
                raise SubmitRejected(_brief_error(exc)) from exc
            raise SubmissionUnknown(
                _brief_error(exc), request_id=getattr(exc, "request_id", None)
            ) from exc

    async def _handle(self, request_id: str) -> object:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id is required")
        client = await self._client_or_raise()
        try:
            return await client.get_handle(KOKORO_MODEL, request_id)
        except AttributeError as exc:
            raise ProviderUnavailable(
                "fal-client AsyncClient.get_handle is required for restart-safe polling"
            ) from exc

    async def poll(self, request_id: str) -> AssetResult:
        try:
            handle = await self._handle(request_id)
            status = await handle.status(with_logs=False)
            name = _status_name(status)
            if name == "Queued":
                position = getattr(status, "position", None)
                return AssetResult(
                    state="queued",
                    queue_position=position if isinstance(position, int) else None,
                )
            if name == "InProgress":
                return AssetResult(state="running")
            if name != "Completed":
                return AssetResult(state="unknown", error=f"Unexpected fal queue status: {name}")
            if getattr(status, "error", None):
                return AssetResult(
                    state="failed", error=_brief_error(RuntimeError(str(status.error)))
                )
            result = await handle.get()
            url = result.get("audio_url") if isinstance(result, dict) else None
            if not isinstance(url, str) or not url:
                audio = result.get("audio") if isinstance(result, dict) else None
                url = audio.get("url") if isinstance(audio, dict) else None
            if not isinstance(url, str) or not url:
                return AssetResult(state="unknown", error="Fal completed without an audio URL")
            return AssetResult(state="ready", url=url)
        except (ProviderUnavailable, ValueError):
            raise
        except Exception as exc:  # noqa: BLE001 - a failed poll cannot establish the job outcome
            return AssetResult(state="unknown", error=_brief_error(exc))
