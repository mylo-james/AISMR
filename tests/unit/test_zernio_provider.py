"""Offline contract tests for the Zernio direct-TikTok adapter."""

from __future__ import annotations

import pytest

from myloware.providers.publishing import PublicationUnknown, PublishRejected
from myloware.providers.zernio import FakeZernioProvider, ZernioProvider


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, object, object]] = []
        self.error: BaseException | None = None

    async def post(self, path: str, *, json: object, headers: object) -> FakeResponse:
        if self.error:
            raise self.error
        self.calls.append(("POST", path, json, headers))
        return self.response

    async def get(self, path: str) -> FakeResponse:
        self.calls.append(("GET", path, None, None))
        return self.response


def post_payload(
    *,
    status: str = "scheduled",
    platform_status: str = "scheduled",
    url: object = None,
    platform_id: object = None,
) -> dict:
    return {
        "post": {
            "_id": "post-123",
            "status": status,
            "platforms": [
                {
                    "platform": "tiktok",
                    "status": platform_status,
                    "platformPostUrl": url,
                    "platformPostId": platform_id,
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_submit_binds_approved_inputs_to_zernio_direct_post_contract() -> None:
    client = FakeClient(FakeResponse(201, post_payload()))
    provider = ZernioProvider(client)
    result = await provider.submit(
        video_url="https://media.example/final.mp4",
        account_id="account-123",
        caption="AISMR pool",
        privacy="PUBLIC_TO_EVERYONE",
        ai_disclosure=True,
        operation_key="visitor-run-1",
    )
    assert result.state == "accepted"
    _, path, body, headers = client.calls[0]
    assert path == "/posts"
    assert body == {
        "content": "AISMR pool",
        "mediaItems": [{"type": "video", "url": "https://media.example/final.mp4"}],
        "platforms": [{"platform": "tiktok", "accountId": "account-123"}],
        "tiktokSettings": {
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "allow_comment": False,
            "allow_duet": False,
            "allow_stitch": False,
            "content_preview_confirmed": True,
            "express_consent_given": True,
            "video_made_with_ai": True,
            "draft": False,
        },
        "publishNow": True,
        "metadata": body["metadata"],
    }
    assert isinstance(headers, dict) and headers["x-request-id"]


@pytest.mark.asyncio
async def test_poll_requires_genuine_tiktok_id_and_url_for_published() -> None:
    client = FakeClient(
        FakeResponse(200, post_payload(status="published", platform_status="published"))
    )
    provider = ZernioProvider(client)
    unresolved = await provider.poll("post-123")
    assert unresolved.state == "publishing"
    client.response = FakeResponse(
        200,
        post_payload(
            status="published",
            platform_status="published",
            platform_id="7420000000000000001",
            url="https://www.tiktok.com/@aismr/video/7420000000000000001",
        ),
    )
    resolved = await provider.poll("post-123")
    assert resolved.state == "published"
    assert resolved.platform_post_id == "7420000000000000001"


@pytest.mark.asyncio
async def test_failed_rejected_and_ambiguous_submission_outcomes_stay_distinct() -> None:
    failed_client = FakeClient(
        FakeResponse(
            207,
            post_payload(status="failed", platform_status="failed"),
        )
    )
    failed = await ZernioProvider(failed_client).submit(
        video_url="https://media.example/a.mp4",
        account_id="a",
        caption="",
        privacy="P",
        ai_disclosure=True,
        operation_key="failed",
    )
    assert failed.state == "failed"

    rejected_client = FakeClient(FakeResponse(400, {"error": "privacy unavailable"}))
    with pytest.raises(PublishRejected, match="privacy unavailable"):
        await ZernioProvider(rejected_client).submit(
            video_url="https://media.example/a.mp4",
            account_id="a",
            caption="",
            privacy="bad",
            ai_disclosure=True,
            operation_key="one",
        )
    unknown_client = FakeClient(FakeResponse(503, {"error": "down"}))
    with pytest.raises(PublicationUnknown, match="down"):
        await ZernioProvider(unknown_client).submit(
            video_url="https://media.example/a.mp4",
            account_id="a",
            caption="",
            privacy="P",
            ai_disclosure=True,
            operation_key="two",
        )


@pytest.mark.asyncio
async def test_fake_adapter_never_invents_a_real_tiktok_receipt() -> None:
    fake = FakeZernioProvider()
    accepted = await fake.submit(
        video_url="https://media.example/a.mp4",
        account_id="configured",
        caption="demo",
        privacy="PUBLIC_TO_EVERYONE",
        ai_disclosure=True,
        operation_key="stable",
    )
    assert accepted.state == "accepted"
    assert (
        accepted.post_id is None
        and accepted.platform_url is None
        and "Simulated" in (accepted.error or "")
    )
    assert (await fake.poll("anything")).state == "unknown"


@pytest.mark.asyncio
async def test_rejects_untrusted_api_origin_and_nonpublic_video_url() -> None:
    with pytest.raises(ValueError, match="origin"):
        ZernioProvider(api_base_url="https://example.com/api/v1")
    provider = ZernioProvider(FakeClient(FakeResponse(201, post_payload())))
    with pytest.raises(ValueError, match="HTTPS"):
        await provider.submit(
            video_url="http://media.example/a.mp4",
            account_id="configured",
            caption="demo",
            privacy="PUBLIC_TO_EVERYONE",
            ai_disclosure=True,
            operation_key="stable",
        )
