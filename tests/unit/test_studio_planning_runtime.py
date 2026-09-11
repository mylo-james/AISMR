from __future__ import annotations

from uuid import uuid4

import pytest

from myloware.config.studio import StudioSettings
from myloware.storage.studio_store import StudioError
from myloware.studio.runtime import build_local_components, build_planning_components
from myloware.studio.service import StudioService
from myloware.workflows.langgraph.studio import _runtime_mode_compatible


def _planning_settings(**overrides: object) -> StudioSettings:
    values: dict[str, object] = {
        "enabled": True,
        "mode": "planning",
        "live_enabled": False,
        "planner_version": "creative-v2",
        "ideation_backend": "codex",
        "OPENAI_API_KEY": "test-openai-key",
        "session_secret": "s" * 32,
        "origin": "http://127.0.0.1:8452",
    }
    values.update(overrides)
    return StudioSettings(**values)


def _local_settings(**overrides: object) -> StudioSettings:
    values: dict[str, object] = {
        "enabled": True,
        "mode": "local",
        "live_enabled": False,
        "planner_version": "creative-v2",
        "ideation_backend": "codex",
        "OPENAI_API_KEY": "test-openai-key",
        "session_secret": "s" * 32,
        "origin": "http://127.0.0.1:8452",
        "local_media_root": "/private/tmp/local-scene-media",
        "render_real": True,
    }
    values.update(overrides)
    return StudioSettings(**values)


def test_planning_readiness_requires_only_private_codex_text_configuration() -> None:
    config = _planning_settings()

    assert config.planning_configuration_errors() == ()
    assert (
        "fal_credentials_missing"
        in StudioSettings(enabled=True, mode="live").live_configuration_errors()
    )


def test_planning_readiness_accepts_secure_tailnet_api_origin_but_not_general_public() -> None:
    tailnet = _planning_settings(
        origin="https://mylos-mac-mini.tail0c4e0a.ts.net:8452", cookie_secure=True
    )
    assert tailnet.planning_configuration_errors() == ()

    public = _planning_settings(origin="https://studio.example.test", cookie_secure=True)
    assert "planning_private_origin_required" in public.planning_configuration_errors()


def test_planning_readiness_requires_explicit_creative_codex_and_disables_live_effects() -> None:
    assert (
        "planning_requires_creative_v2"
        in _planning_settings(planner_version="single-v1").planning_configuration_errors()
    )
    assert (
        "planning_requires_codex"
        in _planning_settings(ideation_backend="openai").planning_configuration_errors()
    )
    assert (
        "planning_live_effects_must_be_disabled"
        in _planning_settings(live_enabled=True).planning_configuration_errors()
    )


def test_planning_worker_composition_is_loopback_only_and_does_not_need_fal_or_renderer() -> None:
    components = build_planning_components(_planning_settings())
    assert components.creative_planner is not None
    assert components.ideator is not None
    assert components.creative_planner._deadline_seconds == 120
    assert components.creative_planner._workflow_deadline_seconds == 660

    tailnet = _planning_settings(
        origin="https://mylos-mac-mini.tail0c4e0a.ts.net:8452", cookie_secure=True
    )
    with pytest.raises(StudioError, match="planning_worker_requires_loopback_origin"):
        build_planning_components(tailnet)


def test_local_runtime_uses_real_text_components_but_rejects_effect_boundaries() -> None:
    components = build_local_components(_local_settings())
    assert components.creative_planner is not None
    assert components.ideator is not None
    assert (
        "local_fal_credentials_forbidden"
        in _local_settings(fal_key="must-not-be-used").local_configuration_errors()
    )
    assert (
        "local_live_effects_must_be_disabled"
        in _local_settings(live_enabled=True).local_configuration_errors()
    )
    assert (
        "local_public_runtime_must_be_disabled"
        in _local_settings(public_runtime_enabled=True).local_configuration_errors()
    )


def test_local_runtime_can_resume_only_legacy_planning_runs() -> None:
    assert _runtime_mode_compatible(configured_mode="local", run_mode="local")
    assert _runtime_mode_compatible(configured_mode="local", run_mode="planning")
    assert not _runtime_mode_compatible(configured_mode="local", run_mode="live")
    assert not _runtime_mode_compatible(configured_mode="local", run_mode="fixture")


@pytest.mark.asyncio
async def test_planning_service_rejects_media_entrypoints_before_any_store_or_provider_use() -> (
    None
):
    service = StudioService(
        type("Store", (), {"config": _planning_settings()})(), moderator=object()
    )

    with pytest.raises(StudioError, match="planning_only"):
        service.assets(uuid4())
    with pytest.raises(StudioError, match="planning_only"):
        await service.submit_edit(uuid4())
    with pytest.raises(StudioError, match="planning_only"):
        await service.inspect_edit(uuid4())
