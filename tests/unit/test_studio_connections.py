from __future__ import annotations

import pytest

from myloware.studio.connections import (
    CreatorCapabilitiesUnavailable,
    ZernioTikTokReadiness,
)


class Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code, self._payload = status_code, payload

    def json(self) -> object:
        return self._payload


class Client:
    def __init__(self, responses: list[Response]) -> None:
        self.responses, self.calls = responses, []

    async def get(self, path: str, **kwargs: object) -> Response:
        self.calls.append((path, kwargs))
        return self.responses.pop(0)


def healthy() -> dict:
    return {
        "accounts": [
            {
                "accountId": "tt-1",
                "status": "healthy",
                "canPost": True,
                "tokenValid": True,
                "needsReconnect": False,
            }
        ]
    }


def creator(*, enabled_stitch: bool = False) -> dict:
    return {
        "creator": {"nickname": "aismr", "canPostMore": True},
        "privacyLevels": [{"value": "PUBLIC_TO_EVERYONE"}, {"value": "SELF_ONLY"}],
        "postingLimits": {
            "maxVideoDurationSec": 600,
            "interactionSettings": {
                "allow_comment": {"required": True, "enabled": True},
                "allow_duet": {"required": True, "enabled": True},
                "allow_stitch": {"required": True, "enabled": enabled_stitch},
            },
        },
        "commercialContentTypes": [{"value": "none"}],
    }


@pytest.mark.asyncio
async def test_reads_current_health_and_creator_limits_then_validates_preflight() -> None:
    client = Client([Response(200, healthy()), Response(200, creator())])
    capabilities = await ZernioTikTokReadiness(client).creator_capabilities("tt-1")
    assert client.calls == [
        ("/accounts/health", {"params": {"platform": "tiktok"}}),
        ("/accounts/tt-1/tiktok/creator-info", {"params": {"mediaType": "video"}}),
    ]
    capabilities.validate_publication(
        account_id="tt-1",
        privacy="PUBLIC_TO_EVERYONE",
        duration_seconds=40.4,
        interactions={"allow_comment": False, "allow_duet": False, "allow_stitch": False},
    )
    with pytest.raises(CreatorCapabilitiesUnavailable, match="disabled"):
        capabilities.validate_publication(
            account_id="tt-1",
            privacy="PUBLIC_TO_EVERYONE",
            duration_seconds=40.4,
            interactions={"allow_comment": False, "allow_duet": False, "allow_stitch": True},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "health_payload",
    [
        {"accounts": []},
        {
            "accounts": [
                {
                    "accountId": "tt-1",
                    "status": "warning",
                    "canPost": True,
                    "tokenValid": True,
                    "needsReconnect": False,
                }
            ]
        },
    ],
)
async def test_health_failure_blocks_before_creator_info(health_payload: dict) -> None:
    client = Client([Response(200, health_payload)])
    with pytest.raises(CreatorCapabilitiesUnavailable, match="not postable"):
        await ZernioTikTokReadiness(client).creator_capabilities("tt-1")
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_partial_or_invalid_creator_limits_fail_closed() -> None:
    broken = creator()
    broken["postingLimits"]["interactionSettings"].pop("allow_stitch")
    client = Client([Response(200, healthy()), Response(200, broken)])
    with pytest.raises(CreatorCapabilitiesUnavailable, match="incomplete"):
        await ZernioTikTokReadiness(client).creator_capabilities("tt-1")
