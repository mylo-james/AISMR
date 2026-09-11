"""Fail-closed text and sampled-frame moderation contracts."""

from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

MODERATION_MODEL = "omni-moderation-latest"
FixtureOutcome = Literal["allow", "deny", "malformed", "outage"]


@dataclass(frozen=True)
class ModerationVerdict:
    safe: bool
    reason: str
    model: str
    coverage: tuple[str, ...]


class Moderator(Protocol):
    async def moderate_text(self, text: str, *, stage: str) -> ModerationVerdict: ...

    async def moderate_images(
        self, frames: tuple[Path, ...], *, stage: str
    ) -> ModerationVerdict: ...


class FixtureModerator:
    """Explicit test outcomes. There is no implicit fake pass mode."""

    def __init__(self, outcome: FixtureOutcome) -> None:
        self.outcome = outcome

    async def moderate_text(self, text: str, *, stage: str) -> ModerationVerdict:
        return self._verdict((f"text:{stage}",))

    async def moderate_images(self, frames: tuple[Path, ...], *, stage: str) -> ModerationVerdict:
        if not frames:
            return ModerationVerdict(False, "no frames supplied", "fixture", ())
        return self._verdict(tuple(f"frame:{frame.name}" for frame in frames))

    def _verdict(self, coverage: tuple[str, ...]) -> ModerationVerdict:
        if self.outcome == "allow":
            return ModerationVerdict(True, "fixture allow", "fixture", coverage)
        if self.outcome == "deny":
            return ModerationVerdict(False, "fixture deny", "fixture", coverage)
        if self.outcome == "malformed":
            return ModerationVerdict(False, "fixture malformed response", "fixture", coverage)
        return ModerationVerdict(False, "fixture moderation outage", "fixture", coverage)


class OpenAIModerator:
    """Live image-capable moderation through the official OpenAI moderation API."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def moderate_text(self, text: str, *, stage: str) -> ModerationVerdict:
        if not isinstance(text, str) or not text.strip():
            return ModerationVerdict(False, "empty moderation text", MODERATION_MODEL, ())
        return await self._moderate([{"type": "text", "text": text}], (f"text:{stage}",))

    async def moderate_images(self, frames: tuple[Path, ...], *, stage: str) -> ModerationVerdict:
        if not frames or any(not frame.is_file() for frame in frames):
            return ModerationVerdict(False, "missing moderation frame", MODERATION_MODEL, ())
        # The live endpoint rejects more than one image per request. Keep
        # every sampled frame covered without turning a failed request into a pass.
        coverage: list[str] = []
        for frame in frames:
            verdict = await self._moderate(
                [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64encode(frame.read_bytes()).decode()}"
                        },
                    }
                ],
                (f"frame:{frame.name}",),
            )
            coverage.extend(verdict.coverage)
            if not verdict.safe:
                return ModerationVerdict(False, verdict.reason, verdict.model, tuple(coverage))
        return ModerationVerdict(True, "moderation passed", MODERATION_MODEL, tuple(coverage))

    async def _moderate(
        self, input_value: list[dict[str, Any]], coverage: tuple[str, ...]
    ) -> ModerationVerdict:
        try:
            response = await self._client.moderations.create(
                model=MODERATION_MODEL, input=input_value
            )
            results = getattr(response, "results", None)
            if (
                not isinstance(results, list)
                or not results
                or any(not isinstance(getattr(result, "flagged", None), bool) for result in results)
            ):
                return ModerationVerdict(
                    False, "malformed moderation response", MODERATION_MODEL, coverage
                )
            if any(result.flagged for result in results):
                return ModerationVerdict(
                    False, "moderation flagged content", MODERATION_MODEL, coverage
                )
            return ModerationVerdict(True, "moderation passed", MODERATION_MODEL, coverage)
        except Exception:  # noqa: BLE001 - any provider failure must fail closed.
            return ModerationVerdict(False, "moderation unavailable", MODERATION_MODEL, coverage)


def build_moderator(
    *,
    mode: Literal["fixture", "live", "off"],
    fixture_outcome: FixtureOutcome | None = None,
    client: Any | None = None,
) -> Moderator:
    """Build an explicit moderation capability; disabled and malformed modes fail closed."""
    if mode == "fixture":
        if fixture_outcome is None:
            raise ValueError(
                "fixture moderation requires an explicit allow, deny, malformed, or outage outcome"
            )
        return FixtureModerator(fixture_outcome)
    if mode == "live":
        if client is None:
            raise ValueError("live moderation requires an initialized OpenAI client")
        return OpenAIModerator(client)
    if mode == "off":
        return FixtureModerator("outage")
    raise ValueError("unknown moderation mode")
