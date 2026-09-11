"""Fail-closed, read-only Zernio TikTok connection capability contracts.

This module deliberately performs no OAuth, upload, post, or retry. The caller
supplies a client whose authentication and lifetime are owned by the live
composition root. A successful result is an observed capability receipt, not
authorization to publish.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ZernioReadClient(Protocol):
    async def get(self, path: str, **kwargs: object) -> object: ...


class CreatorCapabilitiesUnavailable(RuntimeError):
    """Zernio could not provide an unambiguous current creator capability receipt."""


@dataclass(frozen=True)
class CreatorCapabilities:
    """The minimal current facts needed before any AISMR TikTok transfer/post."""

    account_id: str
    nickname: str
    can_post: bool
    privacy_levels: frozenset[str]
    required_interactions: frozenset[str]
    enabled_interactions: frozenset[str]
    max_video_duration_seconds: int
    commercial_content_types: frozenset[str]

    def validate_publication(
        self,
        *,
        account_id: str,
        privacy: str,
        duration_seconds: float,
        interactions: dict[str, bool],
    ) -> None:
        """Reject unstated account limits before a caller can transfer media."""
        expected = {"allow_comment", "allow_duet", "allow_stitch"}
        if account_id != self.account_id or not self.can_post:
            raise CreatorCapabilitiesUnavailable("TikTok account is not currently postable")
        if privacy not in self.privacy_levels:
            raise CreatorCapabilitiesUnavailable("TikTok privacy is not allowed for this creator")
        if (
            not isinstance(duration_seconds, (int, float))
            or not 3 <= duration_seconds <= self.max_video_duration_seconds
        ):
            raise CreatorCapabilitiesUnavailable("video duration is outside the creator limits")
        if set(interactions) != expected or any(
            not isinstance(value, bool) for value in interactions.values()
        ):
            raise CreatorCapabilitiesUnavailable(
                "TikTok interaction controls must be explicit booleans"
            )
        if self.required_interactions != expected:
            raise CreatorCapabilitiesUnavailable(
                "TikTok required interaction fields are incomplete"
            )
        if any(interactions[name] for name in expected - self.enabled_interactions):
            raise CreatorCapabilitiesUnavailable("TikTok interaction is disabled for this creator")


class ZernioTikTokReadiness:
    """Read only health and creator facts, accepting no malformed partial success."""

    def __init__(self, client: ZernioReadClient) -> None:
        self._client = client

    async def creator_capabilities(self, account_id: str) -> CreatorCapabilities:
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("Zernio TikTok account_id is required")
        account_id = account_id.strip()
        health = await self._json("/accounts/health", params={"platform": "tiktok"})
        accounts = health.get("accounts")
        account = (
            next(
                (
                    value
                    for value in accounts
                    if isinstance(value, dict) and value.get("accountId") == account_id
                ),
                None,
            )
            if isinstance(accounts, list)
            else None
        )
        if (
            not isinstance(account, dict)
            or account.get("status") != "healthy"
            or account.get("canPost") is not True
            or account.get("tokenValid") is not True
            or account.get("needsReconnect") is not False
        ):
            raise CreatorCapabilitiesUnavailable("TikTok account health is not postable")
        info = await self._json(
            f"/accounts/{account_id}/tiktok/creator-info",
            params={"mediaType": "video"},
        )
        return self._parse_creator_info(account_id, info)

    async def _json(self, path: str, **kwargs: object) -> dict[str, Any]:
        try:
            response = await self._client.get(path, **kwargs)
            status = getattr(response, "status_code", None)
            payload = response.json()
        except Exception as exc:
            raise CreatorCapabilitiesUnavailable("Zernio readiness check unavailable") from exc
        if not isinstance(status, int) or not 200 <= status < 300 or not isinstance(payload, dict):
            raise CreatorCapabilitiesUnavailable("Zernio readiness check returned invalid data")
        return payload

    @staticmethod
    def _parse_creator_info(account_id: str, payload: dict[str, Any]) -> CreatorCapabilities:
        creator = payload.get("creator")
        limits = payload.get("postingLimits")
        privacy_values = payload.get("privacyLevels")
        interactions = limits.get("interactionSettings") if isinstance(limits, dict) else None
        if (
            not isinstance(creator, dict)
            or creator.get("canPostMore") is not True
            or not isinstance(privacy_values, list)
            or not isinstance(interactions, dict)
        ):
            raise CreatorCapabilitiesUnavailable("Zernio creator capability response is incomplete")
        privacy = frozenset(
            value.get("value")
            for value in privacy_values
            if isinstance(value, dict) and isinstance(value.get("value"), str) and value["value"]
        )
        expected = {"allow_comment", "allow_duet", "allow_stitch"}
        if not privacy or set(interactions) != expected:
            raise CreatorCapabilitiesUnavailable("Zernio creator settings are incomplete")
        required = frozenset(
            key
            for key, value in interactions.items()
            if isinstance(value, dict) and value.get("required") is True
        )
        enabled = frozenset(
            key
            for key, value in interactions.items()
            if isinstance(value, dict) and value.get("enabled") is True
        )
        duration = limits.get("maxVideoDurationSec")
        if required != expected or not isinstance(duration, int) or duration < 3:
            raise CreatorCapabilitiesUnavailable("Zernio creator limits are invalid")
        commercial = payload.get("commercialContentTypes")
        commercial_values = (
            frozenset(
                value.get("value")
                for value in commercial
                if isinstance(value, dict)
                and isinstance(value.get("value"), str)
                and value["value"]
            )
            if isinstance(commercial, list)
            else frozenset()
        )
        return CreatorCapabilities(
            account_id=account_id,
            nickname=str(creator.get("nickname") or "")[:120],
            can_post=True,
            privacy_levels=privacy,
            required_interactions=required,
            enabled_interactions=enabled,
            max_video_duration_seconds=duration,
            commercial_content_types=commercial_values,
        )
