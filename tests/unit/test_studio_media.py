from __future__ import annotations

import asyncio

import httpx
import pytest

from myloware.studio.media import (
    MediaFetchPolicy,
    MediaVerificationError,
    fetch_verified_media,
    validate_media_url,
    verify_media_file,
)


def _policy(*, fixture_mode: bool = False) -> MediaFetchPolicy:
    return MediaFetchPolicy(
        allowed_origins=frozenset({"https://media.example"}),
        asset_byte_cap=100,
        run_byte_cap=200,
        fixture_mode=fixture_mode,
    )


def test_media_url_requires_an_exact_configured_origin() -> None:
    assert validate_media_url(
        "https://media.example/path/video.mp4?token=signed", _policy()
    ).endswith("video.mp4?token=signed")
    for value in (
        "https://media.example.evil/video.mp4",
        "https://user:pass@media.example/video.mp4",
        "file:///private/secret.mp4",
        "http://media.example/video.mp4",
    ):
        with pytest.raises(MediaVerificationError):
            validate_media_url(value, _policy())


def test_loopback_requires_exact_fixture_origin() -> None:
    with pytest.raises(MediaVerificationError):
        MediaFetchPolicy(frozenset({"http://127.0.0.1:9000"}), 1, 1)
    policy = MediaFetchPolicy(frozenset({"http://127.0.0.1:9000"}), 1, 1, fixture_mode=True)
    assert validate_media_url("http://127.0.0.1:9000/video.mp4", policy)
    with pytest.raises(MediaVerificationError):
        validate_media_url("http://localhost:9000/video.mp4", policy)


@pytest.mark.parametrize(
    "value",
    (
        "https://storage.googleapis.com/falserverless/../other-bucket/video.mp4",
        "https://storage.googleapis.com/falserverless/%2e%2e/other-bucket/video.mp4",
        "https://storage.googleapis.com/falserverless/%252e%252e/other-bucket/video.mp4",
    ),
)
def test_storage_path_prefix_rejects_plain_and_encoded_traversal(value: str) -> None:
    policy = MediaFetchPolicy(
        allowed_origins=frozenset({"https://fal.media"}),
        asset_byte_cap=100,
        run_byte_cap=200,
        allowed_path_prefixes=frozenset({"https://storage.googleapis.com/falserverless/"}),
    )
    with pytest.raises(MediaVerificationError, match="origin is not configured"):
        validate_media_url(value, policy)


@pytest.mark.asyncio
async def test_ffmpeg_verifies_short_video_and_wav(tmp_path) -> None:
    video, audio = tmp_path / "short.mp4", tmp_path / "short.wav"
    await _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=16x16:d=0.2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=0.2",
        "-shortest",
        str(video),
    )
    await _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=0.2", str(audio))
    verified_video = await verify_media_file(video)
    verified_audio = await verify_media_file(audio, kind="audio")
    assert (
        verified_video.width == verified_video.height == 16 and len(verified_video.frame_paths) == 3
    )
    assert (
        verified_audio.duration_seconds > 0
        and verified_audio.width == verified_audio.height == 0
        and verified_audio.frame_paths == ()
    )


async def _ffmpeg(*arguments: str) -> None:
    process = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-v", "error", *arguments)
    assert await process.wait() == 0


@pytest.mark.asyncio
async def test_renderer_auth_headers_are_sent_once_and_not_redirected(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://other.example/video.mp4"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "myloware.studio.media.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    policy = MediaFetchPolicy(
        allowed_origins=frozenset({"http://127.0.0.1:9000"}),
        asset_byte_cap=100,
        run_byte_cap=100,
        fixture_mode=True,
    )
    with pytest.raises(MediaVerificationError, match="redirects"):
        await fetch_verified_media(
            url="http://127.0.0.1:9000/video.mp4",
            run_id="00000000-0000-0000-0000-000000000000",
            target_root=tmp_path,
            policy=policy,
            request_headers={"Authorization": "Bearer renderer-token"},
        )
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer renderer-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["nan", "inf", "-inf"])
async def test_nonfinite_audio_duration_is_rejected(monkeypatch, tmp_path, duration) -> None:
    path = tmp_path / "invalid.wav"
    path.write_bytes(b"media")

    async def probe(_path):
        return {"streams": [{"codec_type": "audio", "duration": duration}]}

    monkeypatch.setattr("myloware.studio.media._ffprobe", probe)
    with pytest.raises(MediaVerificationError, match="invalid duration"):
        await verify_media_file(path, kind="audio")
