from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from myloware.providers.tiktok_native import (
    NativeTikTokClient,
    TikTokApiError,
    TikTokInitializedUploadError,
    TikTokUploadUnknown,
    UploadInfo,
    _chunk_layout,
)


def client(handler):  # type: ignore[no-untyped-def]
    return NativeTikTokClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        client_key="client-key",
        client_secret="client-secret",
    )


def ok(data: dict[str, object]) -> dict[str, object]:
    return {"data": data, "error": {"code": "ok", "message": "", "log_id": "log"}}


def test_authorization_url_has_only_approved_direct_post_scopes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request)

    url = client(handler).authorization_url("https://callback.example/tiktok", "state-1")
    query = parse_qs(urlsplit(url).query)

    assert urlsplit(url).scheme == "https"
    assert query == {
        "client_key": ["client-key"],
        "scope": ["user.info.basic,video.publish"],
        "redirect_uri": ["https://callback.example/tiktok"],
        "state": ["state-1"],
        "response_type": ["code"],
        "disable_auto_auth": ["1"],
    }


@pytest.mark.asyncio
async def test_exchange_code_posts_form_and_token_repr_is_safe() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "open_id": "open-id",
                "expires_in": 86400,
                "scope": "user.info.basic,video.publish",
            },
        )

    token = await client(handler).exchange_code("code-secret", "https://callback.example/tiktok")

    assert token.open_id == "open-id" and token.scope == ("user.info.basic", "video.publish")
    assert "secret" not in repr(token)
    assert requests[0].url.path == "/v2/oauth/token/"
    body = parse_qs(requests[0].content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["redirect_uri"] == ["https://callback.example/tiktok"]


@pytest.mark.asyncio
async def test_creator_initialize_and_status_use_documented_direct_post_contract() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("creator_info/query/"):
            return httpx.Response(
                200,
                json=ok(
                    {
                        "creator_username": "aismr698",
                        "creator_nickname": "AISMR",
                        "privacy_level_options": ["SELF_ONLY"],
                        "comment_disabled": False,
                        "duet_disabled": False,
                        "stitch_disabled": False,
                        "max_video_post_duration_sec": 180,
                    }
                ),
            )
        if request.url.path.endswith("video/init/"):
            return httpx.Response(
                200,
                json=ok(
                    {
                        "publish_id": "v_pub_file~v2.1",
                        "upload_url": "https://open-upload.tiktokapis.com/video/?upload_id=1&upload_token=secret",
                    }
                ),
            )
        return httpx.Response(
            200,
            json=ok(
                {
                    "status": "PUBLISH_COMPLETE",
                    "fail_reason": "",
                    "publically_available_post_id": ["123"],
                    "uploaded_bytes": 42,
                }
            ),
        )

    api = client(handler)
    creator = await api.creator_info("access")
    upload = await api.initialize_video("access", 42, "Approved caption")
    status = await api.status("access", upload.publish_id)

    assert creator.privacy_level_options == ("SELF_ONLY",)
    assert upload.chunk_size == upload.video_size == 42 and upload.total_chunk_count == 1
    assert "secret" not in repr(upload)
    assert status.status == "PUBLISH_COMPLETE" and status.public_post_ids == ("123",)
    init = json.loads(requests[1].content)
    assert requests[1].url.path == "/v2/post/publish/video/init/"
    assert init["source_info"] == {
        "source": "FILE_UPLOAD",
        "video_size": 42,
        "chunk_size": 42,
        "total_chunk_count": 1,
    }
    assert init["post_info"] == {
        "title": "Approved caption",
        "privacy_level": "SELF_ONLY",
        "disable_comment": True,
        "disable_duet": True,
        "disable_stitch": True,
        "is_aigc": True,
        "brand_content_toggle": False,
        "brand_organic_toggle": False,
        "video_cover_timestamp_ms": 0,
    }
    assert requests[2].url.path == "/v2/post/publish/status/fetch/"


@pytest.mark.asyncio
async def test_stream_upload_sends_range_without_bearer_or_redirects() -> None:
    source = BytesIO(b"approved-bytes")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201)

    upload = UploadInfo(
        "publish-id",
        "https://open-upload.tiktokapis.com/video/?upload_id=1&upload_token=secret",
        14,
        1,
        14,
    )
    await client(handler).upload_stream(upload, source)

    assert requests[0].method == "PUT"
    assert requests[0].headers["content-range"] == "bytes 0-13/14"
    assert requests[0].headers["content-length"] == "14"
    assert "authorization" not in requests[0].headers
    assert requests[0].content == b"approved-bytes"


@pytest.mark.asyncio
async def test_multipart_stream_upload_sends_full_final_remainder() -> None:
    chunk = 32 * 1024 * 1024
    size = chunk * 2 + 4

    class SnapshotStream:
        def __init__(self) -> None:
            self.position = 0

        def seek(self, offset: int, whence: int = 0) -> int:
            self.position = size + offset if whence == 2 else offset
            return self.position

        def tell(self) -> int:
            return self.position

        def read(self, length: int) -> bytes:
            start = self.position
            self.position += length
            if start == 0:
                return b"a" * length
            return b"b" * (length - 4) + b"tail"

    observed: list[tuple[str, int, bytes, bool]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(
            (
                request.headers["content-range"],
                len(request.content),
                request.content[-4:],
                "authorization" in request.headers,
            )
        )
        return httpx.Response(206 if len(observed) == 1 else 201)

    upload = UploadInfo(
        "publish-id",
        "https://open-upload.tiktokapis.com/video/?upload_id=1&upload_token=secret",
        chunk,
        2,
        size,
    )
    await client(handler).upload_stream(upload, SnapshotStream())  # type: ignore[arg-type]

    assert observed == [
        (f"bytes 0-{chunk - 1}/{size}", chunk, b"aaaa", False),
        (f"bytes {chunk}-{size - 1}/{size}", chunk + 4, b"tail", False),
    ]


@pytest.mark.asyncio
async def test_actual_live_size_initializes_with_multiple_32mib_chunks() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=ok(
                {
                    "publish_id": "v_pub_file~v2.1",
                    "upload_url": "https://upload-region-1.tiktokapis.com/video/?upload_id=1&upload_token=secret",
                }
            ),
        )

    size = 92_455_913
    upload = await client(handler).initialize_video("access", size, "Approved caption")

    assert upload.chunk_size == 32 * 1024 * 1024
    assert upload.total_chunk_count == 2
    assert json.loads(requests[0].content)["source_info"] == {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": 32 * 1024 * 1024,
        "total_chunk_count": 2,
    }


@pytest.mark.asyncio
async def test_observed_us_upload_host_is_accepted_without_exposing_query() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=ok(
                {
                    "publish_id": "v_pub_file~v2-1.7683616048247130126",
                    "upload_url": "https://open-upload.tiktokapis.us/video/?upload_id=1&upload_token=secret",
                }
            ),
        )

    upload = await client(handler).initialize_video("access", 42, "Approved caption")

    assert upload.publish_id == "v_pub_file~v2-1.7683616048247130126"
    assert "secret" not in repr(upload)


@pytest.mark.asyncio
async def test_init_preserves_publish_id_when_returned_upload_url_is_unusable() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=ok(
                {
                    "publish_id": "v_pub_file~v2.reconcile-me",
                    "upload_url": "https://attacker.example/video?upload_token=secret",
                }
            ),
        )

    with pytest.raises(TikTokInitializedUploadError) as failure:
        await client(handler).initialize_video("access", 42, "Approved caption")

    assert failure.value.publish_id == "v_pub_file~v2.reconcile-me"
    assert failure.value.reason_code == "upload_url_untrusted"
    assert failure.value.upload_host == "attacker.example"
    assert "secret" not in str(failure.value)
    assert [request.method for request in calls] == ["POST"]


@pytest.mark.asyncio
async def test_api_error_and_upload_failures_do_not_expose_provider_body(tmp_path: Path) -> None:
    async def api_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error": {
                    "code": "scope_not_authorized",
                    "message": "secret body",
                    "log_id": "safe-log",
                }
            },
        )

    with pytest.raises(TikTokApiError, match="scope_not_authorized") as error:
        await client(api_error).creator_info("access")
    assert "secret body" not in str(error.value)
    assert error.value.status_code == 200 and error.value.log_id == "safe-log"

    path = tmp_path / "approved.mp4"
    path.write_bytes(b"bytes")

    async def failed_upload(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"provider secret body")

    upload = UploadInfo("publish", "https://open-upload.tiktokapis.com/video/?x=secret", 5, 1, 5)
    with pytest.raises(TikTokUploadUnknown) as failed:
        await client(failed_upload).upload_video(upload, path)
    assert "secret" not in str(failed.value)


@pytest.mark.asyncio
async def test_chunk_rules_and_untrusted_upload_urls_are_rejected(tmp_path: Path) -> None:
    assert _chunk_layout(4 * 1024 * 1024) == (4 * 1024 * 1024, 1)
    assert _chunk_layout(50 * 1024 * 1024) == (50 * 1024 * 1024, 1)
    assert _chunk_layout(64 * 1024 * 1024) == (64 * 1024 * 1024, 1)
    size = 65 * 1024 * 1024
    chunk, count = _chunk_layout(size)
    assert count == 2 and chunk == 32 * 1024 * 1024
    with pytest.raises(ValueError):
        _chunk_layout(1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="4 GiB"):
        _chunk_layout(4 * 1024 * 1024 * 1024 + 1)

    path = tmp_path / "approved.mp4"
    path.write_bytes(b"bytes")
    upload = UploadInfo("publish", "https://evil.example/upload", 5, 1, 5)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request)

    with pytest.raises(ValueError, match="approved HTTPS TikTok host"):
        await client(handler).upload_video(upload, path)

    for url in (
        "http://upload-region-1.tiktokapis.com/video",
        "https://tiktokapis.com/video",
        "https://upload-region-1.tiktokapis.com:444/video",
        "https://127.0.0.1/video",
        "https://lookalike.tiktokapis.us/video",
        "https://upload-region-1.tiktokapis.com/video#fragment",
    ):
        with pytest.raises(ValueError, match="approved HTTPS TikTok host"):
            await client(handler).upload_video(UploadInfo("publish", url, 5, 1, 5), path)
