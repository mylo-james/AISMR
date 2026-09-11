"""Short-lived live composition for one monthly studio graph stage.

This module only builds local clients.  It does not probe accounts or submit
provider work during startup, so callers can expose configuration readiness
without creating paid effects.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from myloware.config.studio import StudioSettings
from myloware.storage.studio_store import StudioError
from myloware.studio.codex_ideation_client import CodexIdeationClient
from myloware.studio.creative_planning import CreativePlanner
from myloware.studio.ideation import MonthlyIdeator
from myloware.studio.moderation import Moderator, build_moderator
from myloware.studio.telemetry import LoadedKnowledgeSource


@dataclass(frozen=True)
class LiveConfigurationReadiness:
    """Effect-free configuration result, distinct from an observed account receipt."""

    ready: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class LiveComponents:
    ideator: MonthlyIdeator
    moderator: Moderator
    closeables: tuple[object, ...]
    creative_planner: CreativePlanner | None = None


def live_configuration_readiness(config: StudioSettings) -> LiveConfigurationReadiness:
    errors = config.live_configuration_errors()
    return LiveConfigurationReadiness(ready=not errors, errors=errors)


def build_live_components(config: StudioSettings) -> LiveComponents:
    readiness = live_configuration_readiness(config)
    if not readiness.ready:
        raise StudioError("live_runtime_not_ready", 503)
    knowledge_sources = _monthly_knowledge()
    knowledge = tuple(source.text for source in knowledge_sources)
    from openai import AsyncOpenAI

    moderation_client = AsyncOpenAI(api_key=config.openai_key.get_secret_value(), max_retries=0)
    moderator = build_moderator(mode="live", client=moderation_client)
    if config.ideation_backend == "codex":
        # The transport has a stricter subprocess timeout.  The outer stage
        # deadline remains explicit in configuration for orchestration.
        client = CodexIdeationClient(
            codex_bin=str(config.codex_path),
            model=config.codex_model,
            timeout_seconds=min(config.ideation_deadline_seconds, 90),
            max_input_bytes=96_000,
        )
        return LiveComponents(
            ideator=MonthlyIdeator(
                client,
                knowledge=knowledge,
                knowledge_sources=knowledge_sources,
                model=config.codex_model,
            ),
            moderator=moderator,
            closeables=(moderation_client,),
            creative_planner=CreativePlanner(
                client,
                model=config.codex_model,
                deadline_seconds=config.ideation_deadline_seconds,
                workflow_deadline_seconds=config.creative_workflow_deadline_seconds,
            ),
        )
    return LiveComponents(
        ideator=MonthlyIdeator(
            moderation_client,
            knowledge=knowledge,
            knowledge_sources=knowledge_sources,
            model=config.openai_ideation_model,
        ),
        moderator=moderator,
        closeables=(moderation_client,),
        creative_planner=CreativePlanner(
            moderation_client,
            model=config.openai_ideation_model,
            deadline_seconds=config.ideation_deadline_seconds,
            workflow_deadline_seconds=config.creative_workflow_deadline_seconds,
        ),
    )


def build_planning_components(config: StudioSettings) -> LiveComponents:
    """Compose private real text planning without media-provider capabilities."""
    errors = config.planning_configuration_errors()
    if errors:
        raise StudioError("planning_runtime_not_ready", 503)
    # The public Tailnet API may admit a planning review, but the worker that
    # invokes the local Codex CLI must be separately configured on loopback.
    if not config.is_loopback_origin():
        raise StudioError("planning_worker_requires_loopback_origin", 503)
    knowledge_sources = _monthly_knowledge()
    knowledge = tuple(source.text for source in knowledge_sources)
    from openai import AsyncOpenAI

    moderation_client = AsyncOpenAI(
        api_key=config.openai_key.get_secret_value(), max_retries=0, timeout=30.0
    )
    client = CodexIdeationClient(
        codex_bin=str(config.codex_path),
        model=config.codex_model,
        timeout_seconds=min(config.ideation_deadline_seconds, 90),
        max_input_bytes=96_000,
    )
    return LiveComponents(
        ideator=MonthlyIdeator(
            client,
            knowledge=knowledge,
            knowledge_sources=knowledge_sources,
            model=config.codex_model,
        ),
        moderator=build_moderator(mode="live", client=moderation_client),
        closeables=(moderation_client,),
        creative_planner=CreativePlanner(
            client,
            model=config.codex_model,
            deadline_seconds=config.ideation_deadline_seconds,
            workflow_deadline_seconds=config.creative_workflow_deadline_seconds,
        ),
    )


def build_local_components(config: StudioSettings) -> LiveComponents:
    """Compose private real text agents for local reused-media rehearsal runs."""
    errors = config.local_configuration_errors()
    if errors:
        raise StudioError("local_runtime_not_ready", 503)
    if not config.is_loopback_origin():
        raise StudioError("local_worker_requires_loopback_origin", 503)
    planning_config = config.model_copy(update={"mode": "planning"})
    return build_planning_components(planning_config)


def _monthly_knowledge() -> tuple[LoadedKnowledgeSource, ...]:
    """Read only the approved ideator role and its concept-planning knowledge."""
    root = Path("data")
    sources = (
        root / "projects/monthly/agents/ideator.yaml",
        root / "knowledge/ideation/concept-planning.md",
    )
    return tuple(
        LoadedKnowledgeSource(source=path.as_posix(), text=path.read_text(encoding="utf-8"))
        for path in sources
        if path.is_file()
    )


@asynccontextmanager
async def open_live_ideator(config: StudioSettings) -> AsyncIterator[MonthlyIdeator]:
    """Release an async fallback client at the end of the owning graph stage."""
    components = build_live_components(config)
    try:
        yield components.ideator
    finally:
        for client in components.closeables:
            close = getattr(client, "aclose", None)
            if callable(close):
                await close()
