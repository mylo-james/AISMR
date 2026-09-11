"""Offline HTTP contracts for TikTok inbox delivery without public posting."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from myloware.providers.zernio_inbox import ZernioInboxProvider


def receipt(
    status: object = "published",
    *,
    details: object = None,
    account: object = "account-1",
    post_id: object = "post-1",
) -> dict[str, Any]:
    return {
        "post": {
            "_id": post_id,
            "status": status,
            "platforms": [
                {
                    "platform": "tiktok",
                    "accountId": account,
                    "platformPostId": "v_inbox_url~v2.12345",
                    "status": status,
                    "platformSpecificData": ({"isDraft": True} if details is None else details),
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_inbox_upload_uses_tiktok_draft_flag_and_preserves_consent() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json=receipt())

    async with httpx.AsyncClient(
        base_url="https://zernio.com/api/v1", transport=httpx.MockTransport(handler)
    ) as client:
        result = await ZernioInboxProvider(client).upload_draft(
            video_url="https://media.example/final.mp4",
            account_id="account-1",
            caption="Approved caption",
            ai_disclosure=True,
            operation_key="owner-draft-1",
        )
    assert result.state == "provider_accepted"
    assert result.post_id == "post-1"
    assert result.tiktok_publish_id == "v_inbox_url~v2.12345"
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://zernio.com/api/v1/posts"
    body = json.loads(request.content)
    assert body["tiktokSettings"] == {
        "draft": True,
        "privacy_level": "SELF_ONLY",
        "allow_comment": False,
        "allow_duet": False,
        "allow_stitch": False,
        "content_preview_confirmed": True,
        "express_consent_given": True,
        "video_made_with_ai": True,
    }
    assert body["publishNow"] is True and body["isDraft"] is False
    assert body["platforms"] == [{"platform": "tiktok", "accountId": "account-1"}]
    assert body["content"] == "Approved caption"
    assert request.headers["x-request-id"]
    assert len(body["metadata"]["operationKeyDigest"]) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (receipt("queued"), "uploading"),
        (receipt("processing"), "uploading"),
        (receipt("failed"), "failed"),
        (receipt(details={"isDraft": False}), "unknown"),
        (receipt(details={}), "unknown"),
        (receipt(details="invalid"), "unknown"),
        (receipt(details={"isDraft": "true"}), "unknown"),
        (receipt(account="someone-else"), "unknown"),
        (receipt(account={"_id": "account-1"}), "provider_accepted"),
        (receipt(post_id="wrong-id"), "unknown"),
        ({"post": {"_id": "post-1", "platforms": None}}, "unknown"),
        ({"existingPost": receipt()["post"]}, "provider_accepted"),
        ({"post": {"_id": "post-1"}}, "unknown"),
        ({}, "unknown"),
    ],
)
async def test_poll_classifies_receipts_without_claiming_publication(
    data: object, expected: str
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(
        base_url="https://zernio.com/api/v1", transport=httpx.MockTransport(handler)
    ) as client:
        result = await ZernioInboxProvider(client).poll_upload("post-1", account_id="account-1")
    assert result.state == expected
    assert result.post_id == "post-1"
    assert len(requests) == 1 and requests[0].method == "GET"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("timeout", "unknown"),
        ("503", "unknown"),
        ("429", "unknown"),
        ("400", "failed"),
        ("json", "unknown"),
    ],
)
async def test_submit_failure_is_never_automatically_retried(mode: str, expected: str) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if mode == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        if mode == "json":
            return httpx.Response(200, content=b"not json")
        return httpx.Response(int(mode), json={"error": "provider error"})

    async with httpx.AsyncClient(
        base_url="https://zernio.com/api/v1", transport=httpx.MockTransport(handler)
    ) as client:
        result = await ZernioInboxProvider(client).upload_draft(
            video_url="https://media.example/final.mp4",
            account_id="account-1",
            caption="",
            ai_disclosure=True,
            operation_key="one-intent",
        )
    assert result.state == expected and calls == 1


@pytest.mark.asyncio
async def test_poll_escapes_post_id_and_rejects_different_receipt() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=receipt())

    async with httpx.AsyncClient(
        base_url="https://zernio.com/api/v1", transport=httpx.MockTransport(handler)
    ) as client:
        result = await ZernioInboxProvider(client).poll_upload("post/other?x=1")
    assert calls[0].url.raw_path == b"/api/v1/posts/post%2Fother%3Fx%3D1"
    assert result.state == "unknown" and result.post_id == "post/other?x=1"
