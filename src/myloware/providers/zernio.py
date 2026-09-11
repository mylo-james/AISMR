"""Bounded Zernio direct-post adapter for one configured TikTok demo account."""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx

from myloware.providers.publishing import (
    PublicationResult,
    PublicationUnknown,
    PublishingUnavailable,
    PublishRejected,
)

ZERNIO_API_BASE_URL = "https://zernio.com/api/v1"
_MAX_ERROR_CHARS = 300
_SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[^\s,;]+", re.IGNORECASE),
    re.compile(r"sk_[0-9a-f]{64}", re.IGNORECASE),
)


def _brief_error(value: object) -> str:
    if value is None:
        return "Provider request failed"
    text = " ".join(str(value).split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text[:_MAX_ERROR_CHARS] or "Provider request failed"


def _operation_request_id(operation_key: str) -> str:
    if not isinstance(operation_key, str) or not operation_key.strip():
        raise ValueError("operation_key is required")
    try:
        return str(uuid.UUID(operation_key))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aismr:zernio:{operation_key}"))


def _validate_video_url(video_url: str) -> str:
    parsed = urlparse(video_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("video_url must be a public HTTPS URL without credentials")
    return video_url


def _is_tiktok_url(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "tiktok.com" or host.endswith(".tiktok.com"))


def _find_tiktok_platform(post: dict[str, Any]) -> dict[str, Any] | None:
    platforms = post.get("platforms")
    if not isinstance(platforms, list):
        return None
    for platform in platforms:
        if isinstance(platform, dict) and platform.get("platform") == "tiktok":
            return platform
    return None


def _normalize_post(payload: object, *, from_submit: bool) -> PublicationResult:
    post = payload.get("post") if isinstance(payload, dict) else None
    if not isinstance(post, dict):
        return PublicationResult(
            state="unknown", error="Zernio response did not contain a post receipt"
        )
    post_id = post.get("_id")
    post_id = post_id if isinstance(post_id, str) and post_id else None
    platform = _find_tiktok_platform(post)
    if platform is None:
        return PublicationResult(
            state="unknown", post_id=post_id, error="Zernio post has no TikTok receipt"
        )
    platform_status = platform.get("status")
    if platform_status == "failed" or post.get("status") == "failed":
        error = platform.get("errorMessage") or platform.get("error") or payload.get("error")
        return PublicationResult(state="failed", post_id=post_id, error=_brief_error(error))
    if platform.get("platformSpecificData", {}).get("isDraft") is True:
        return PublicationResult(
            state="failed",
            post_id=post_id,
            error="TikTok Creator Inbox drafts do not satisfy direct publication",
        )
    platform_post_id = platform.get("platformPostId") or platform.get("platform_post_id")
    platform_post_id = (
        platform_post_id if isinstance(platform_post_id, str) and platform_post_id else None
    )
    platform_url = platform.get("platformPostUrl") or platform.get("publishedUrl")
    platform_url = platform_url if _is_tiktok_url(platform_url) else None
    if platform_status == "published" and platform_post_id and platform_url:
        return PublicationResult(
            state="published",
            post_id=post_id,
            platform_post_id=platform_post_id,
            platform_url=platform_url,
        )
    if platform_status == "published":
        return PublicationResult(
            state="publishing",
            post_id=post_id,
            platform_post_id=platform_post_id,
            error="TikTok publication is unresolved until a platform post ID and TikTok URL are available",
        )
    if platform_status in {
        "publishing",
        "processing",
        "scheduled",
        "queued",
    } or post.get("status") in {
        "publishing",
        "processing",
        "scheduled",
        "queued",
    }:
        return PublicationResult(state="accepted" if from_submit else "publishing", post_id=post_id)
    return PublicationResult(
        state="unknown", post_id=post_id, error="Unexpected Zernio publication status"
    )


class ZernioProvider:
    """Submit a visitor-approved MP4 to a configured TikTok account only."""

    def __init__(
        self,
        client: httpx.AsyncClient | object | None = None,
        *,
        api_key: str | None = None,
        api_base_url: str = ZERNIO_API_BASE_URL,
    ) -> None:
        if api_base_url.rstrip("/") != ZERNIO_API_BASE_URL:
            raise ValueError("Zernio API origin must be https://zernio.com/api/v1")
        self._client = client
        self._api_key = api_key
        self._owns_client = client is None

    async def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise PublishingUnavailable("Zernio API key is required for real publishing")
        self._client = httpx.AsyncClient(
            base_url=ZERNIO_API_BASE_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(20.0),
        )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            close = getattr(self._client, "aclose", None)
            if callable(close):
                await close()

    async def creator_capabilities(self, account_id: str) -> object:
        """Read current TikTok posting limits without uploading or posting media."""
        from myloware.studio.connections import ZernioTikTokReadiness

        return await ZernioTikTokReadiness(await self._client_or_raise()).creator_capabilities(
            account_id
        )

    async def submit(
        self,
        *,
        video_url: str,
        account_id: str,
        caption: str,
        privacy: str,
        ai_disclosure: bool,
        operation_key: str,
    ) -> PublicationResult:
        video_url = _validate_video_url(video_url)
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("account_id is required")
        if not isinstance(caption, str) or len(caption) > 2200:
            raise ValueError("caption must be a string up to 2200 characters")
        if not isinstance(privacy, str) or not privacy.strip():
            raise ValueError("privacy is required")
        if not isinstance(ai_disclosure, bool):
            raise TypeError("ai_disclosure must be a boolean")
        request_id = _operation_request_id(operation_key)
        body = {
            "content": caption,
            "mediaItems": [{"type": "video", "url": video_url}],
            "platforms": [{"platform": "tiktok", "accountId": account_id.strip()}],
            "tiktokSettings": {
                "privacy_level": privacy.strip(),
                "allow_comment": False,
                "allow_duet": False,
                "allow_stitch": False,
                "content_preview_confirmed": True,
                "express_consent_given": True,
                "video_made_with_ai": ai_disclosure,
                "draft": False,
            },
            "publishNow": True,
            "metadata": {"operationKeyDigest": hashlib.sha256(operation_key.encode()).hexdigest()},
        }
        try:
            client = await self._client_or_raise()
            response = await client.post("/posts", json=body, headers={"x-request-id": request_id})
        except PublishingUnavailable:
            raise
        except Exception as exc:
            raise PublicationUnknown(_brief_error(exc)) from exc
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - provider payload is untrusted
            payload = {}
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 429:
            raise PublishRejected(
                _brief_error(
                    payload.get("error") if isinstance(payload, dict) else "request rejected"
                )
            )
        if not isinstance(status_code, int) or status_code < 200 or status_code >= 300:
            raise PublicationUnknown(
                _brief_error(
                    payload.get("error") if isinstance(payload, dict) else "request unknown"
                )
            )
        return _normalize_post(payload, from_submit=True)

    async def poll(self, post_id: str) -> PublicationResult:
        if not isinstance(post_id, str) or not post_id.strip():
            raise ValueError("post_id is required")
        try:
            client = await self._client_or_raise()
            response = await client.get(f"/posts/{post_id}")
        except PublishingUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - retain an unknown outcome after polling fails
            return PublicationResult(state="unknown", post_id=post_id, error=_brief_error(exc))
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - provider payload is untrusted
            return PublicationResult(
                state="unknown", post_id=post_id, error="Invalid Zernio status response"
            )
        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int) or status_code < 200 or status_code >= 300:
            return PublicationResult(
                state="unknown",
                post_id=post_id,
                error="Unable to reconcile Zernio post",
            )
        return _normalize_post(payload, from_submit=False)


class FakeZernioProvider:
    """Explicit simulation. It never returns a TikTok URL or post identifier."""

    async def submit(
        self,
        *,
        video_url: str,
        account_id: str,
        caption: str,
        privacy: str,
        ai_disclosure: bool,
        operation_key: str,
    ) -> PublicationResult:
        _validate_video_url(video_url)
        _operation_request_id(operation_key)
        if (
            not account_id
            or not isinstance(caption, str)
            or not privacy
            or not isinstance(ai_disclosure, bool)
        ):
            raise ValueError("Invalid simulated publication input")
        return PublicationResult(
            state="accepted", error="Simulated publication: no TikTok post was created"
        )

    async def poll(self, post_id: str) -> PublicationResult:
        return PublicationResult(state="unknown", error="Simulated publication has no TikTok post")
