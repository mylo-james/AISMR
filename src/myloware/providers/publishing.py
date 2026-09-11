"""Provider-neutral publication receipts and failure categories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PublicationState = Literal["rejected", "accepted", "publishing", "published", "failed", "unknown"]


@dataclass(frozen=True)
class PublicationResult:
    """Bounded state for one configured-account TikTok publication."""

    state: PublicationState
    post_id: str | None = None
    platform_post_id: str | None = None
    platform_url: str | None = None
    error: str | None = None


class PublishingUnavailable(RuntimeError):
    """The configured real publishing service is unavailable locally."""


class PublishRejected(RuntimeError):
    """A definitive provider rejection that did not create a post."""


class PublicationUnknown(RuntimeError):
    """A submission may have been accepted and must be reconciled before retrying."""

    def __init__(self, message: str, *, post_id: str | None = None) -> None:
        super().__init__(message)
        self.post_id = post_id
