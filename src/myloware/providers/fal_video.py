"""fal FastWan text-to-video adapter.

This module only submits and observes queue jobs. Persistence, budget reservation,
and workflow transitions remain outside this provider seam.
"""

from __future__ import annotations

from typing import Any

from myloware.providers.media import (
    AssetResult,
    ProviderUnavailable,
    Submission,
    SubmissionUnknown,
    SubmitRejected,
)

WAN_FAST_MODEL = "fal-ai/wan/v2.2-5b/text-to-video/fast-wan"


def _brief_error(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    return f"Provider HTTP {status}" if isinstance(status, int) else exc.__class__.__name__


def _status_name(value: object) -> str:
    return value.__class__.__name__


def _is_definitive_rejection(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    return isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 429


class FalVideoProvider:
    """Async FastWan adapter using the maintained fal-client queue API."""

    def __init__(self, client: object | None = None, *, key: str | None = None) -> None:
        self._client: Any | None = client
        self._key = key

    async def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import fal_client  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on installation
            raise ProviderUnavailable("fal-client is required for the Fal video provider") from exc
        from myloware.providers.fal_transport import FalQueueClient

        self._client = FalQueueClient(key=self._key)
        return self._client

    async def submit(self, *, prompt: str, ordinal: int, operation_key: str) -> Submission:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 1 <= ordinal <= 12:
            raise ValueError("ordinal must be an integer from 1 through 12")
        if not isinstance(operation_key, str) or not operation_key.strip():
            raise ValueError("operation_key is required")
        arguments: dict[str, object] = {
            "prompt": prompt.strip(),
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
        try:
            client = await self._client_or_raise()
            # Disable fal's server-side retries. A local retry without an accepted
            # request ID would otherwise make paid-work idempotency ambiguous.
            handle = await client.submit(
                WAN_FAST_MODEL, arguments=arguments, headers={"X-Fal-No-Retry": "1"}
            )
            request_id = getattr(handle, "request_id", None)
            if not isinstance(request_id, str) or not request_id:
                raise SubmissionUnknown("Fal accepted an invalid submission receipt")
            return Submission(request_id=request_id, model=WAN_FAST_MODEL)
        except (ProviderUnavailable, SubmissionUnknown, ValueError):
            raise
        except Exception as exc:
            if _is_definitive_rejection(exc):
                raise SubmitRejected(_brief_error(exc)) from exc
            raise SubmissionUnknown(
                _brief_error(exc), request_id=getattr(exc, "request_id", None)
            ) from exc

    async def _handle(self, request_id: str) -> Any:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id is required")
        client = await self._client_or_raise()
        try:
            return await client.get_handle(WAN_FAST_MODEL, request_id)
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
            error = getattr(status, "error", None)
            if error:
                return AssetResult(state="failed", error=_brief_error(RuntimeError(str(error))))
            result: Any = await handle.get()
            video = result.get("video") if isinstance(result, dict) else None
            url = video.get("url") if isinstance(video, dict) else None
            if not isinstance(url, str) or not url:
                return AssetResult(state="unknown", error="Fal completed without a video URL")
            return AssetResult(state="ready", url=url)
        except (ProviderUnavailable, ValueError):
            raise
        except Exception as exc:  # noqa: BLE001 - a failed poll cannot establish the job outcome
            return AssetResult(state="unknown", error=_brief_error(exc))

    async def cancel(self, request_id: str) -> None:
        handle = await self._handle(request_id)
        await handle.cancel()

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if callable(close):
            await close()
