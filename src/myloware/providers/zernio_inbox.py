"""Zernio TikTok inbox delivery, separate from public publication receipts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

import httpx

from myloware.providers.zernio import (
    ZernioProvider,
    _brief_error,
    _operation_request_id,
    _validate_video_url,
)

InboxState = Literal["accepted", "uploading", "provider_accepted", "failed", "unknown"]


@dataclass(frozen=True)
class InboxUploadResult:
    """Provider acceptance is not proof of TikTok inbox-notification delivery."""

    state: InboxState
    post_id: str | None = None
    error: str | None = None
    tiktok_publish_id: str | None = None


def _receipt(
    payload: object, *, submitted: bool, account_id: str | None = None
) -> InboxUploadResult:
    if not isinstance(payload, dict):
        return InboxUploadResult("unknown", error="Zernio response has no post receipt")
    post = payload.get("post") or payload.get("existingPost")
    if not isinstance(post, dict):
        return InboxUploadResult("unknown", error="Zernio response has no post receipt")
    post_id = post.get("_id")
    post_id = post_id if isinstance(post_id, str) and post_id.strip() else None
    platforms = post.get("platforms")
    tiktok = (
        [p for p in platforms if isinstance(p, dict) and p.get("platform") == "tiktok"]
        if isinstance(platforms, list)
        else []
    )
    if not post_id or len(tiktok) != 1:
        return InboxUploadResult("unknown", post_id, "Zernio draft receipt is incomplete")
    platform = tiktok[0]
    actual_account = platform.get("accountId")
    if isinstance(actual_account, dict):
        actual_account = actual_account.get("_id")
    if account_id is not None and actual_account != account_id:
        return InboxUploadResult("unknown", post_id, "Zernio receipt account mismatch")
    status = platform.get("status")
    if status == "failed" or post.get("status") == "failed":
        return InboxUploadResult(
            "failed",
            post_id,
            _brief_error(
                platform.get("errorMessage") or platform.get("error") or payload.get("error")
            ),
        )
    if status == "published":
        details = platform.get("platformSpecificData")
        publish_id = platform.get("platformPostId")
        if (
            isinstance(details, dict)
            and details.get("isDraft") is True
            and isinstance(publish_id, str)
            and publish_id.startswith("v_inbox_")
        ):
            # Zernio reports acceptance here. TikTok's SEND_TO_USER_INBOX
            # status or native inbox-delivered webhook is separate evidence.
            return InboxUploadResult("provider_accepted", post_id, tiktok_publish_id=publish_id)
        return InboxUploadResult(
            "unknown", post_id, "Zernio receipt lacks TikTok inbox draft proof"
        )
    if status in ("queued", "scheduled"):
        return InboxUploadResult("accepted" if submitted else "uploading", post_id)
    if status in ("processing", "uploading", "publishing"):
        return InboxUploadResult("uploading", post_id)
    return InboxUploadResult("unknown", post_id, "Unexpected Zernio draft status")


class ZernioInboxProvider(ZernioProvider):
    """One caller-authorized draft request; callers own persistence and recovery."""

    async def upload_draft(
        self,
        *,
        video_url: str,
        account_id: str,
        caption: str,
        ai_disclosure: bool,
        operation_key: str,
        privacy: str = "SELF_ONLY",
    ) -> InboxUploadResult:
        video_url = _validate_video_url(video_url)
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("account_id is required")
        account_id = account_id.strip()
        if not isinstance(caption, str) or len(caption) > 2200:
            raise ValueError("caption must be a string up to 2200 characters")
        if not isinstance(ai_disclosure, bool):
            raise TypeError("ai_disclosure must be a boolean")
        if not isinstance(privacy, str) or not privacy.strip():
            raise ValueError("privacy is required")
        request_id = _operation_request_id(operation_key)
        body = {
            "content": caption,
            "mediaItems": [{"type": "video", "url": video_url}],
            "platforms": [{"platform": "tiktok", "accountId": account_id}],
            "tiktokSettings": {
                "draft": True,
                "privacy_level": privacy.strip(),
                "allow_comment": False,
                "allow_duet": False,
                "allow_stitch": False,
                "content_preview_confirmed": True,
                "express_consent_given": True,
                "video_made_with_ai": ai_disclosure,
            },
            # isDraft at the root would save only in Zernio. The nested flag
            # above requests delivery into TikTok's inbox now.
            "publishNow": True,
            "isDraft": False,
            "metadata": {"operationKeyDigest": hashlib.sha256(operation_key.encode()).hexdigest()},
        }
        try:
            response = await (await self._client_or_raise()).post(
                "/posts", json=body, headers={"x-request-id": request_id}
            )
        except (httpx.HTTPError, OSError) as exc:
            return InboxUploadResult("unknown", error=_brief_error(exc))
        try:
            payload = response.json()
        except ValueError:
            payload = None
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or not 200 <= status < 300:
            error = _brief_error(payload.get("error") if isinstance(payload, dict) else None)
            if isinstance(status, int) and 400 <= status < 500 and status not in (408, 409, 429):
                return InboxUploadResult("failed", error=error)
            return InboxUploadResult("unknown", error=error)
        return _receipt(payload, submitted=True, account_id=account_id)

    async def poll_upload(
        self, post_id: str, *, account_id: str | None = None
    ) -> InboxUploadResult:
        if not isinstance(post_id, str) or not post_id.strip():
            raise ValueError("post_id is required")
        try:
            response = await (await self._client_or_raise()).get(
                f"/posts/{quote(post_id, safe='')}"
            )
        except (httpx.HTTPError, OSError) as exc:
            return InboxUploadResult("unknown", post_id, _brief_error(exc))
        try:
            payload = response.json()
        except ValueError:
            return InboxUploadResult("unknown", post_id, "Invalid Zernio status response")
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or not 200 <= status < 300:
            return InboxUploadResult("unknown", post_id, "Unable to reconcile Zernio draft")
        result = _receipt(payload, submitted=False, account_id=account_id)
        if result.post_id != post_id:
            return InboxUploadResult("unknown", post_id, "Zernio poll receipt ID mismatch")
        return result
