from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from myloware.studio.moderation import (
    MODERATION_MODEL,
    OpenAIModerator,
    build_moderator,
)


@pytest.mark.asyncio
async def test_fixture_moderation_requires_explicit_outcome_and_fails_closed() -> None:
    with pytest.raises(ValueError):
        build_moderator(mode="fixture")
    denied = await build_moderator(mode="fixture", fixture_outcome="deny").moderate_text(
        "input", stage="ideation"
    )
    outage = await build_moderator(mode="off").moderate_text("input", stage="caption")
    assert denied.safe is False
    assert outage.safe is False


@pytest.mark.asyncio
async def test_live_moderation_accepts_only_unflagged_well_formed_results() -> None:
    class Moderations:
        async def create(self, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs["model"] == MODERATION_MODEL
            return SimpleNamespace(results=[SimpleNamespace(flagged=False)])

    verdict = await OpenAIModerator(SimpleNamespace(moderations=Moderations())).moderate_text(
        "safe input", stage="ideation"
    )
    assert verdict.safe is True
    assert verdict.coverage == ("text:ideation",)


@pytest.mark.asyncio
async def test_live_moderation_rejects_flagged_or_malformed_responses() -> None:
    class Flagged:
        async def create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(results=[SimpleNamespace(flagged=True)])

    class Malformed:
        async def create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(results=[SimpleNamespace(flagged="false")])

    assert not (
        await OpenAIModerator(SimpleNamespace(moderations=Flagged())).moderate_text(
            "x", stage="prompt"
        )
    ).safe
    assert not (
        await OpenAIModerator(SimpleNamespace(moderations=Malformed())).moderate_images(
            (Path(__file__),), stage="video"
        )
    ).safe


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_second", [False, True])
async def test_images_are_individual_requests_and_failed_frame_stops_coverage(
    tmp_path, reject_second
):
    frames = tuple(tmp_path / f"{index}.jpg" for index in range(3))
    for frame in frames:
        frame.write_bytes(b"fixture image bytes")
    calls = []

    class SingleImageEndpoint:
        async def create(self, **kwargs):
            assert len(kwargs["input"]) == 1
            calls.append(kwargs)
            return SimpleNamespace(
                results=[SimpleNamespace(flagged=reject_second and len(calls) == 2)]
            )

    result = await OpenAIModerator(
        SimpleNamespace(moderations=SingleImageEndpoint())
    ).moderate_images(frames, stage="final")
    assert result.safe is (not reject_second)
    assert len(calls) == (2 if reject_second else 3)
    assert result.coverage == tuple(f"frame:{frame.name}" for frame in frames[: len(calls)])
