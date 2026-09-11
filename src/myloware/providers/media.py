"""Shared contracts and deterministic offline media providers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

AssetState = Literal["queued", "running", "ready", "failed", "unknown"]


@dataclass(frozen=True)
class Submission:
    """A provider request accepted by the local adapter."""

    request_id: str
    model: str


@dataclass(frozen=True)
class AssetResult:
    """The bounded, provider-neutral state of one generated asset."""

    state: AssetState
    url: str | None = None
    error: str | None = None
    queue_position: int | None = None
    # A batch narration provider returns its audio only with the word-level
    # receipt required for fail-closed local silence splitting.
    timestamps: tuple[dict[str, Any], ...] = ()
    input_text: str | None = None
    # Local replay adapters expose an immutable archive receipt separately from
    # provider results. This is never a claim of remote generation.
    local_media: dict[str, Any] | None = None


class ProviderUnavailable(RuntimeError):
    """Raised when a configured real provider cannot be used locally."""


class SubmitRejected(RuntimeError):
    """A definitive pre-acceptance request rejection, safe to correct and retry."""


class SubmissionUnknown(RuntimeError):
    """Submission outcome is uncertain and must not be blindly repeated."""

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


_FAKE_ID = re.compile(r"^fake-(video|voice)-(\d{2})-([0-9a-f]{16})$")
_FAKE_BATCH_ID = re.compile(r"^fake-voice-batch-([0-9a-f]{16})$")
FAKE_NARRATION_BATCH_MODEL = "fake-voice-batch"
LOCAL_SCENE_MEDIA_MODEL = "local-saved-scene-v1"
_LOCAL_VIDEO_ID = re.compile(r"^local-scene-video-(\d{2})-([0-9a-f]{16})$")
_LOCAL_BATCH_ID = re.compile(r"^local-scene-title-batch-([0-9a-f]{16})$")


def fake_request_id(*, kind: Literal["video", "voice"], ordinal: int, operation_key: str) -> str:
    """Return an ordinal-preserving ID that can be polled after process restart."""

    _validate_submission_inputs(ordinal=ordinal, operation_key=operation_key)
    digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:16]
    return f"fake-{kind}-{ordinal:02d}-{digest}"


def fake_ready_result(
    *,
    kind: Literal["video", "voice"],
    request_id: str,
    trusted_fixture_base_url: str,
    scene_mode: bool = False,
) -> AssetResult:
    """Map a deterministic fake ID to a fixture asset without mutable process state."""

    match = _FAKE_ID.fullmatch(request_id)
    if match is None or match.group(1) != kind:
        return AssetResult(state="unknown", error="Unknown fake media request")
    base = trusted_fixture_base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ProviderUnavailable("Fake media provider requires an HTTP(S) fixture base URL")
    if scene_mode:
        suffix = ".mp4" if kind == "video" else "-title.wav"
        return AssetResult(state="ready", url=f"{base}/scene-{match.group(2)}{suffix}")
    suffix = "mp4" if kind == "video" else "wav"
    return AssetResult(state="ready", url=f"{base}/month-{match.group(2)}.{suffix}")


def _validate_submission_inputs(*, ordinal: int, operation_key: str) -> None:
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 1 <= ordinal <= 12:
        raise ValueError("ordinal must be an integer from 1 through 12")
    if not isinstance(operation_key, str) or not operation_key.strip():
        raise ValueError("operation_key is required")


class FakeVideoProvider:
    """Deterministic fixture-only video provider for fast workflow tests."""

    model = "fake-video"

    def __init__(self, *, trusted_fixture_base_url: str, scene_mode: bool = False) -> None:
        self.trusted_fixture_base_url = trusted_fixture_base_url
        self.scene_mode = scene_mode

    async def submit(self, *, prompt: str, ordinal: int, operation_key: str) -> Submission:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        return Submission(
            request_id=fake_request_id(kind="video", ordinal=ordinal, operation_key=operation_key),
            model=self.model,
        )

    async def poll(self, request_id: str) -> AssetResult:
        return fake_ready_result(
            kind="video",
            request_id=request_id,
            trusted_fixture_base_url=self.trusted_fixture_base_url,
            scene_mode=self.scene_mode,
        )

    async def cancel(self, request_id: str) -> None:
        if _FAKE_ID.fullmatch(request_id) is None:
            raise ValueError("Unknown fake media request")


class FakeVoiceProvider:
    """Deterministic fixture-only narration provider for fast workflow tests."""

    model = "fake-voice"

    def __init__(self, *, trusted_fixture_base_url: str) -> None:
        self.trusted_fixture_base_url = trusted_fixture_base_url

    async def submit(self, *, prompt: str, ordinal: int, operation_key: str) -> Submission:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        return Submission(
            request_id=fake_request_id(kind="voice", ordinal=ordinal, operation_key=operation_key),
            model=self.model,
        )

    async def poll(self, request_id: str) -> AssetResult:
        return fake_ready_result(
            kind="voice",
            request_id=request_id,
            trusted_fixture_base_url=self.trusted_fixture_base_url,
        )

    async def cancel(self, request_id: str) -> None:
        if _FAKE_ID.fullmatch(request_id) is None:
            raise ValueError("Unknown fake media request")


class FakeNarrationBatchProvider:
    """One deterministic fixture narration receipt with caller-supplied proof."""

    model = FAKE_NARRATION_BATCH_MODEL

    def __init__(
        self,
        *,
        trusted_fixture_base_url: str,
        timestamps: tuple[dict[str, Any], ...],
        scene_mode: bool = False,
    ) -> None:
        if not timestamps:
            raise ValueError("fixture batch timestamps are required")
        self.trusted_fixture_base_url = trusted_fixture_base_url.rstrip("/")
        self.timestamps = timestamps
        self.scene_mode = scene_mode

    async def submit_batch(
        self,
        *,
        lines: tuple[str, ...],
        operation_key: str,
        voice_profile: dict[str, object] | None = None,
    ) -> Submission:
        if len(lines) != 12 or not all(isinstance(line, str) and line.strip() for line in lines):
            raise ValueError("fixture narration batch requires twelve lines")
        digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:16]
        return Submission(f"fake-voice-batch-{digest}", self.model)

    async def poll(self, request_id: str) -> AssetResult:
        if _FAKE_BATCH_ID.fullmatch(request_id) is None:
            return AssetResult(state="unknown", error="Unknown fake narration batch")
        return AssetResult(
            state="ready",
            url=f"{self.trusted_fixture_base_url}/{'title-batch.wav' if self.scene_mode else 'voice-batch.wav'}",
            timestamps=self.timestamps,
        )


class LocalSceneVideoProvider:
    """Expose a hash-bound private archive through the normal video provider seam."""

    model = LOCAL_SCENE_MEDIA_MODEL

    def __init__(self, *, archive: Any, trusted_local_base_url: str) -> None:
        self.archive = archive
        self.trusted_local_base_url = _trusted_base_url(trusted_local_base_url)

    async def submit(self, *, prompt: str, ordinal: int, operation_key: str) -> Submission:
        _validate_submission_inputs(ordinal=ordinal, operation_key=operation_key)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        self.archive.video(ordinal)
        digest = hashlib.sha256(operation_key.encode()).hexdigest()[:16]
        return Submission(f"local-scene-video-{ordinal:02d}-{digest}", self.model)

    async def poll(self, request_id: str) -> AssetResult:
        match = _LOCAL_VIDEO_ID.fullmatch(request_id)
        if match is None:
            return AssetResult(state="unknown", error="Unknown local scene video request")
        ordinal = int(match.group(1))
        source = self.archive.video(ordinal)
        return AssetResult(
            state="ready",
            url=f"{self.trusted_local_base_url}/video/{ordinal:02d}-video.mp4",
            local_media={
                "archive": self.archive.receipt(),
                "source_title": source.source_title,
                "source_sha256": source.sha256,
            },
        )


class LocalSceneNarrationBatchProvider:
    """Return existing title-only audio with its actual source-text receipt."""

    model = LOCAL_SCENE_MEDIA_MODEL

    def __init__(self, *, archive: Any, trusted_local_base_url: str) -> None:
        self.archive = archive
        self.trusted_local_base_url = _trusted_base_url(trusted_local_base_url)

    async def submit_batch(
        self,
        *,
        lines: tuple[str, ...],
        operation_key: str,
        voice_profile: dict[str, object] | None = None,
    ) -> Submission:
        if len(lines) != 12 or not all(isinstance(line, str) and line.strip() for line in lines):
            raise ValueError("local scene narration requires twelve requested title lines")
        if not isinstance(operation_key, str) or not operation_key.strip():
            raise ValueError("operation_key is required")
        self.archive.batch()
        digest = hashlib.sha256(operation_key.encode()).hexdigest()[:16]
        return Submission(f"local-scene-title-batch-{digest}", self.model)

    async def poll(self, request_id: str) -> AssetResult:
        if _LOCAL_BATCH_ID.fullmatch(request_id) is None:
            return AssetResult(state="unknown", error="Unknown local scene narration request")
        batch = self.archive.batch()
        return AssetResult(
            state="ready",
            url=f"{self.trusted_local_base_url}/voice/title-batch.wav",
            timestamps=self.archive.timestamps(),
            input_text="\n\n".join(self.archive.source_title_lines()),
            local_media={
                "archive": self.archive.receipt(),
                "source_batch_sha256": batch.sha256,
                "source_title_lines": list(self.archive.source_title_lines()),
            },
        )


class RecordedVideoProvider:
    """Map verified archived video assets to a run-scoped internal URL surface."""

    model = "recorded-approved-v3-video"

    def __init__(self, *, base_url: str) -> None:
        self.base_url = _trusted_base_url(base_url)

    async def submit(self, *, prompt: str, ordinal: int, operation_key: str) -> Submission:
        _validate_submission_inputs(ordinal=ordinal, operation_key=operation_key)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        return Submission(
            f"recorded-video-{ordinal:02d}-{hashlib.sha256(operation_key.encode()).hexdigest()[:16]}",
            self.model,
        )

    async def poll(self, request_id: str) -> AssetResult:
        match = re.fullmatch(r"recorded-video-(\d{2})-[0-9a-f]{16}", request_id)
        if match is None:
            return AssetResult(state="unknown", error="Unknown recorded video request")
        return AssetResult(
            state="ready", url=f"{self.base_url}/recorded/video/{match.group(1)}-video.mp4"
        )


class RecordedNarrationBatchProvider:
    """Map a checked approved batch and its checked timestamp receipt to local URLs."""

    model = "recorded-approved-v3-eleven-v3"

    def __init__(
        self,
        *,
        base_url: str,
        timestamps: tuple[dict[str, Any], ...],
        input_text: str | None = None,
    ) -> None:
        if not timestamps:
            raise ValueError("recorded narration timestamps are required")
        self.base_url = _trusted_base_url(base_url)
        self.timestamps = timestamps
        self.input_text = input_text

    async def submit_batch(
        self,
        *,
        lines: tuple[str, ...],
        operation_key: str,
        voice_profile: dict[str, object] | None = None,
    ) -> Submission:
        if len(lines) != 12:
            raise ValueError("recorded narration batch requires twelve lines")
        return Submission(
            f"recorded-voice-batch-{hashlib.sha256(operation_key.encode()).hexdigest()[:16]}",
            self.model,
        )

    async def poll(self, request_id: str) -> AssetResult:
        if re.fullmatch(r"recorded-voice-batch-[0-9a-f]{16}", request_id) is None:
            return AssetResult(state="unknown", error="Unknown recorded narration request")
        return AssetResult(
            state="ready",
            url=f"{self.base_url}/recorded/voice/original-batch.mp3",
            timestamps=self.timestamps,
            input_text=self.input_text,
        )


def _trusted_base_url(value: str) -> str:
    base = value.rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ProviderUnavailable("Recorded provider requires an HTTP(S) internal base URL")
    return base
