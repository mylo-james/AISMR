"""Small, owner-operated client for TikTok Login Kit and private Direct Post.

The caller owns OAuth state, token persistence, creator-facing review, durable
publish intent, and recovery.  This module deliberately has no retries: a lost
response to an init or upload request leaves the effect uncertain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final
from urllib.parse import urlencode, urlsplit

import httpx

_API_BASE: Final = "https://open.tiktokapis.com"
_AUTHORIZE_URL: Final = "https://www.tiktok.com/v2/auth/authorize/"
_MIN_CHUNK_BYTES: Final = 5 * 1024 * 1024
_MAX_CHUNK_BYTES: Final = 64 * 1024 * 1024
_MAX_CHUNKS: Final = 1000
_MAX_VIDEO_BYTES: Final = 4 * 1024 * 1024 * 1024
_MULTIPART_CHUNK_BYTES: Final = 32 * 1024 * 1024
_OBSERVED_UPLOAD_HOSTS: Final = frozenset({"open-upload.tiktokapis.us"})


class TikTokNativeError(RuntimeError):
    """Base exception that never includes credential or provider-body text."""


class TikTokApiError(TikTokNativeError):
    def __init__(
        self, code: str, status_code: int | None = None, log_id: str | None = None
    ) -> None:
        self.code = code if code else "unknown_error"
        self.status_code = status_code
        self.log_id = log_id
        super().__init__(f"TikTok API request failed: {self.code}")


class TikTokProtocolError(TikTokNativeError):
    """TikTok returned a successful transport response with an unusable shape."""


class TikTokUploadUnknown(TikTokNativeError):
    """A FILE_UPLOAD mutation may have taken effect but did not complete clearly."""


class TikTokInitializedUploadError(TikTokNativeError):
    """An initialized publish ID exists, but its returned upload URL is unusable."""

    def __init__(self, publish_id: str, reason_code: str, upload_host: str | None = None) -> None:
        self.publish_id = publish_id
        self.reason_code = reason_code
        self.upload_host = upload_host
        super().__init__(f"TikTok upload initialization is incomplete: {reason_code}")


@dataclass(frozen=True)
class TikTokToken:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    open_id: str
    expires_in: int
    scope: tuple[str, ...]


@dataclass(frozen=True)
class CreatorInfo:
    creator_username: str
    creator_nickname: str
    privacy_level_options: tuple[str, ...]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_video_post_duration_sec: int


@dataclass(frozen=True)
class UploadInfo:
    publish_id: str
    upload_url: str = field(repr=False)
    chunk_size: int
    total_chunk_count: int
    video_size: int


@dataclass(frozen=True)
class PostStatus:
    status: str
    fail_reason: str | None
    public_post_ids: tuple[str, ...]
    uploaded_bytes: int | None


class NativeTikTokClient:
    """Thin API client with an injected ``httpx.AsyncClient`` for testability."""

    def __init__(self, client: httpx.AsyncClient, *, client_key: str, client_secret: str) -> None:
        if not client_key or not client_secret:
            raise ValueError("TikTok client credentials are required")
        self._client = client
        self._client_key = client_key
        self._client_secret = client_secret

    def authorization_url(self, redirect_uri: str, state: str) -> str:
        _require_https_url(redirect_uri, "redirect URI")
        if not state:
            raise ValueError("OAuth state is required")
        return (
            _AUTHORIZE_URL
            + "?"
            + urlencode(
                {
                    "client_key": self._client_key,
                    "scope": "user.info.basic,video.publish",
                    "redirect_uri": redirect_uri,
                    "state": state,
                    "response_type": "code",
                    "disable_auto_auth": "1",
                }
            )
        )

    async def exchange_code(self, code: str, redirect_uri: str) -> TikTokToken:
        if not code:
            raise ValueError("OAuth code is required")
        _require_https_url(redirect_uri, "redirect URI")
        response = await self._request(
            "POST",
            "/v2/oauth/token/",
            data={
                "client_key": self._client_key,
                "client_secret": self._client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = _payload(response)
        return TikTokToken(
            access_token=_required_string(payload, "access_token"),
            refresh_token=_required_string(payload, "refresh_token"),
            open_id=_required_string(payload, "open_id"),
            expires_in=_required_int(payload, "expires_in"),
            scope=tuple(
                value.strip()
                for value in _required_string(payload, "scope").split(",")
                if value.strip()
            ),
        )

    async def creator_info(self, access_token: str) -> CreatorInfo:
        payload = await self._api_json("/v2/post/publish/creator_info/query/", access_token, {})
        data = _data(payload)
        options = data.get("privacy_level_options")
        if not isinstance(options, list) or not all(isinstance(value, str) for value in options):
            raise TikTokProtocolError("TikTok creator information is incomplete")
        return CreatorInfo(
            creator_username=_required_string(data, "creator_username"),
            creator_nickname=_required_string(data, "creator_nickname"),
            privacy_level_options=tuple(options),
            comment_disabled=_required_bool(data, "comment_disabled"),
            duet_disabled=_required_bool(data, "duet_disabled"),
            stitch_disabled=_required_bool(data, "stitch_disabled"),
            max_video_post_duration_sec=_required_int(data, "max_video_post_duration_sec"),
        )

    async def initialize_video(
        self,
        access_token: str,
        video_size: int,
        title: str,
        privacy_level: str = "SELF_ONLY",
        is_aigc: bool = True,
        disable_comment: bool = True,
        disable_duet: bool = True,
        disable_stitch: bool = True,
        brand_content_toggle: bool = False,
        brand_organic_toggle: bool = False,
        video_cover_timestamp_ms: int | None = 0,
    ) -> UploadInfo:
        if not access_token or not title or len(title) > 2200:
            raise ValueError("access token and a title of at most 2200 characters are required")
        if privacy_level != "SELF_ONLY":
            raise ValueError("this owner-operated client permits SELF_ONLY Direct Posts only")
        if not all(
            isinstance(value, bool)
            for value in (
                is_aigc,
                disable_comment,
                disable_duet,
                disable_stitch,
                brand_content_toggle,
                brand_organic_toggle,
            )
        ):
            raise ValueError("TikTok post toggles must be booleans")
        if video_cover_timestamp_ms is not None and (
            not isinstance(video_cover_timestamp_ms, int)
            or isinstance(video_cover_timestamp_ms, bool)
            or video_cover_timestamp_ms < 0
        ):
            raise ValueError("video cover timestamp must be a non-negative integer")
        chunk_size, total_chunk_count = _chunk_layout(video_size)
        post_info: dict[str, object] = {
            "title": title,
            "privacy_level": privacy_level,
            "disable_comment": disable_comment,
            "disable_duet": disable_duet,
            "disable_stitch": disable_stitch,
            "is_aigc": is_aigc,
            "brand_content_toggle": brand_content_toggle,
            "brand_organic_toggle": brand_organic_toggle,
        }
        if video_cover_timestamp_ms is not None:
            post_info["video_cover_timestamp_ms"] = video_cover_timestamp_ms
        payload = await self._api_json(
            "/v2/post/publish/video/init/",
            access_token,
            {
                "post_info": post_info,
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": video_size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": total_chunk_count,
                },
            },
        )
        data = _data(payload)
        publish_id = _required_string(data, "publish_id")
        try:
            upload_url = _required_string(data, "upload_url")
        except TikTokProtocolError as exc:
            raise TikTokInitializedUploadError(publish_id, "upload_url_missing") from exc
        try:
            _require_upload_url(upload_url)
        except ValueError as exc:
            raise TikTokInitializedUploadError(
                publish_id, "upload_url_untrusted", _upload_hostname(upload_url)
            ) from exc
        return UploadInfo(
            publish_id=publish_id,
            upload_url=upload_url,
            chunk_size=chunk_size,
            total_chunk_count=total_chunk_count,
            video_size=video_size,
        )

    async def upload_video(self, upload_info: UploadInfo, path: Path) -> None:
        try:
            with path.open("rb") as source:
                await self.upload_stream(upload_info, source)
        except OSError as exc:
            raise ValueError("video file is unavailable") from exc

    async def upload_stream(self, upload_info: UploadInfo, source: BinaryIO) -> None:
        """Upload one already-open reviewed snapshot without retaining its pathname."""
        _require_upload_url(upload_info.upload_url)
        try:
            source.seek(0, 2)
            file_size = source.tell()
            source.seek(0)
        except (AttributeError, OSError) as exc:
            raise ValueError("video source must be a seekable binary stream") from exc
        if not isinstance(file_size, int) or isinstance(file_size, bool):
            raise TypeError("video source size is invalid")
        if file_size != upload_info.video_size:
            raise ValueError("video size differs from initialized upload")
        if _chunk_layout(file_size) != (upload_info.chunk_size, upload_info.total_chunk_count):
            raise ValueError("upload chunk layout differs from initialized upload")
        try:
            for index in range(upload_info.total_chunk_count):
                start = index * upload_info.chunk_size
                remaining = file_size - start
                length = (
                    remaining
                    if index == upload_info.total_chunk_count - 1
                    else upload_info.chunk_size
                )
                chunk = source.read(length)
                if len(chunk) != length:
                    raise TikTokUploadUnknown("TikTok upload file changed during transfer")
                response = await self._client.put(
                    upload_info.upload_url,
                    content=chunk,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(length),
                        "Content-Range": f"bytes {start}-{start + length - 1}/{file_size}",
                    },
                    follow_redirects=False,
                    timeout=httpx.Timeout(300.0, connect=20.0),
                )
                expected = 201 if index == upload_info.total_chunk_count - 1 else 206
                if response.status_code != expected:
                    raise TikTokUploadUnknown("TikTok upload outcome is unknown")
        except TikTokNativeError:
            raise
        except (OSError, httpx.HTTPError) as exc:
            raise TikTokUploadUnknown("TikTok upload outcome is unknown") from exc

    async def status(self, access_token: str, publish_id: str) -> PostStatus:
        if not publish_id:
            raise ValueError("publish ID is required")
        payload = await self._api_json(
            "/v2/post/publish/status/fetch/", access_token, {"publish_id": publish_id}
        )
        data = _data(payload)
        post_ids = data.get(
            "publically_available_post_id", data.get("publicaly_available_post_id", [])
        )
        if not isinstance(post_ids, list) or not all(isinstance(value, str) for value in post_ids):
            raise TikTokProtocolError("TikTok status post IDs are invalid")
        reason = data.get("fail_reason")
        if reason is not None and not isinstance(reason, str):
            raise TikTokProtocolError("TikTok status failure reason is invalid")
        uploaded = data.get("uploaded_bytes")
        if uploaded is not None and (not isinstance(uploaded, int) or isinstance(uploaded, bool)):
            raise TikTokProtocolError("TikTok status uploaded bytes are invalid")
        return PostStatus(_required_string(data, "status"), reason, tuple(post_ids), uploaded)

    async def _api_json(
        self, path: str, access_token: str, body: dict[str, object]
    ) -> dict[str, object]:
        if not access_token:
            raise ValueError("access token is required")
        response = await self._request(
            "POST",
            path,
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        payload = _payload(response)
        error = payload.get("error")
        if not isinstance(error, dict) or error.get("code") != "ok":
            raise TikTokApiError(_error_code(error), response.status_code, _log_id(error))
        return payload

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            response = await self._client.request(
                method, _API_BASE + path, follow_redirects=False, **kwargs
            )
        except httpx.HTTPError as exc:
            raise TikTokNativeError("TikTok API transport failed") from exc
        if not 200 <= response.status_code < 300:
            code, log_id = _response_error(response)
            raise TikTokApiError(code, response.status_code, log_id)
        return response


def _chunk_layout(video_size: int) -> tuple[int, int]:
    if not isinstance(video_size, int) or isinstance(video_size, bool) or video_size <= 0:
        raise ValueError("video size must be positive")
    if video_size > _MAX_VIDEO_BYTES:
        raise ValueError("video exceeds TikTok's 4 GiB limit")
    if video_size <= _MAX_CHUNK_BYTES:
        return video_size, 1
    # TikTok requires multiple chunks above 64 MiB.  Fixed 32 MiB regular
    # chunks keep the floor-count contract and make the final trailing request
    # at most 64 MiB for a video within TikTok's 4 GiB limit.
    chunk_size = min(video_size, _MULTIPART_CHUNK_BYTES)
    # TikTok defines total_chunk_count as floor(video_size / chunk_size).  The
    # final request carries all remaining bytes and can exceed chunk_size.
    count = video_size // chunk_size
    if count > _MAX_CHUNKS:
        raise ValueError("video requires too many chunks")
    if not _MIN_CHUNK_BYTES <= chunk_size <= _MAX_CHUNK_BYTES:
        raise ValueError("video chunk layout is invalid")
    return chunk_size, count


def _require_https_url(value: str, label: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be an HTTPS URL")


def _require_upload_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("TikTok upload URL is not an approved HTTPS TikTok host") from exc
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or (not hostname.endswith(".tiktokapis.com") and hostname not in _OBSERVED_UPLOAD_HOSTS)
        or port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("TikTok upload URL is not an approved HTTPS TikTok host")


def _upload_hostname(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        return parsed.hostname
    except ValueError:
        return None


def _payload(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise TikTokProtocolError("TikTok response is not JSON") from exc
    if not isinstance(payload, dict):
        raise TikTokProtocolError("TikTok response is not an object")
    return payload


def _response_error(response: httpx.Response) -> tuple[str, str | None]:
    try:
        payload = response.json()
    except ValueError:
        return "http_error", None
    error = payload.get("error") if isinstance(payload, dict) else None
    return _error_code(error), _log_id(error)


def _error_code(error: object) -> str:
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"][:80]
    return "api_error"


def _log_id(error: object) -> str | None:
    if not isinstance(error, dict):
        return None
    value = error.get("log_id", error.get("logid"))
    return value[:128] if isinstance(value, str) and value else None


def _data(payload: dict[str, object]) -> dict[str, object]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TikTokProtocolError("TikTok response data is invalid")
    return data


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise TikTokProtocolError(f"TikTok response {key} is invalid")
    return value


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TikTokProtocolError(f"TikTok response {key} is invalid")
    return value


def _required_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise TikTokProtocolError(f"TikTok response {key} is invalid")
    return value
