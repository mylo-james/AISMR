"""One timestamped Eleven V3 narration request for a monthly plan.

Submission intent, budget, downloads and local splitting belong to the caller.
An unknown result is never converted into permission to generate again.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from myloware.providers.fal_video import (
    FalVideoProvider,
    _brief_error,
    _is_definitive_rejection,
    _status_name,
)
from myloware.providers.media import (
    AssetState,
    ProviderUnavailable,
    Submission,
    SubmissionUnknown,
    SubmitRejected,
)

ELEVEN_V3_MODEL = "fal-ai/elevenlabs/tts/eleven-v3"


@dataclass(frozen=True)
class NarrationBatchResult:
    state: AssetState
    url: str | None = None
    timestamps: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    queue_position: int | None = None


class FalNarrationBatchProvider(FalVideoProvider):
    """Submit all twelve whispered lines once and retain returned word timings."""

    def __init__(
        self,
        client: object | None = None,
        *,
        key: str | None = None,
        voice: str = "KmnvDXRA0HU55Q0aqkPG",
    ) -> None:
        super().__init__(client, key=key)
        if not isinstance(voice, str) or not voice.strip():
            raise ValueError("voice is required")
        self.voice = voice.strip()

    async def submit_batch(
        self,
        *,
        lines: Sequence[str],
        operation_key: str,
        voice_profile: Mapping[str, object] | object | None = None,
    ) -> Submission:
        """Submit one immutable batch from either a legacy or pinned profile.

        Existing v1 receipts retain their historical builder and adapter default.
        A v2 caller must supply the run-pinned profile, which is validated before
        the one outbound request is made.
        """
        from myloware.studio.voice_batch import build_narration_text, build_scene_narration_text

        if voice_profile is None:
            model = ELEVEN_V3_MODEL
            text = build_narration_text(lines)
            arguments = {
                "text": text,
                "voice": self.voice,
                "timestamps": True,
                "stability": 0.5,
                "language_code": "en",
                "apply_text_normalization": "off",
            }
        else:
            from myloware.studio.execution_profile import VoiceProfile

            profile = VoiceProfile.model_validate(voice_profile)
            model = profile.model
            text = build_scene_narration_text(
                lines,
                delivery_cue=profile.delivery_prefix,
                between_cues=profile.between_cues,
            )
            arguments = {
                "text": text,
                "voice": profile.voice,
                "timestamps": profile.timestamps,
                "stability": profile.stability,
                "language_code": profile.language_code,
                "apply_text_normalization": profile.apply_text_normalization,
            }
        if not isinstance(operation_key, str) or not operation_key.strip():
            raise ValueError("operation_key is required")
        try:
            client = await self._client_or_raise()
            handle = await client.submit(
                model,
                arguments=arguments,
                headers={"X-Fal-No-Retry": "1"},
            )
            request_id = getattr(handle, "request_id", None)
            if not isinstance(request_id, str) or not request_id:
                raise SubmissionUnknown("Fal accepted an invalid submission receipt")
            return Submission(request_id=request_id, model=model)
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
        return await client.get_handle(ELEVEN_V3_MODEL, request_id)

    async def poll(self, request_id: str) -> NarrationBatchResult:
        try:
            handle = await self._handle(request_id)
            status = await handle.status(with_logs=False)
            name = _status_name(status)
            if name == "Queued":
                position = getattr(status, "position", None)
                return NarrationBatchResult(
                    state="queued",
                    queue_position=position if isinstance(position, int) else None,
                )
            if name == "InProgress":
                return NarrationBatchResult(state="running")
            if name != "Completed":
                return NarrationBatchResult(state="unknown", error="Unexpected Fal queue status")
            if getattr(status, "error", None):
                return NarrationBatchResult(state="failed", error="Fal narration generation failed")
            result = await handle.get()
            audio = result.get("audio") if isinstance(result, dict) else None
            url = audio.get("url") if isinstance(audio, dict) else None
            timestamps = result.get("timestamps") if isinstance(result, dict) else None
            if not isinstance(url, str) or not url:
                return NarrationBatchResult(
                    state="unknown", error="Fal completed without an audio URL"
                )
            if (
                not isinstance(timestamps, list)
                or not timestamps
                or len(timestamps) > 4096
                or any(not isinstance(word, dict) for word in timestamps)
            ):
                return NarrationBatchResult(
                    state="unknown",
                    error="Fal completed without usable word timestamps",
                )
            return NarrationBatchResult(state="ready", url=url, timestamps=tuple(timestamps))
        except (ProviderUnavailable, ValueError):
            raise
        except Exception as exc:  # noqa: BLE001 - provider polling must fail closed
            return NarrationBatchResult(state="unknown", error=_brief_error(exc))
