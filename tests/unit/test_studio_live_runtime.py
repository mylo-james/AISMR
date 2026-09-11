from __future__ import annotations

import pytest

from myloware.config.studio import StudioSettings
from myloware.studio.ideation import _bounded_knowledge
from myloware.studio.media import MediaFetchPolicy, MediaVerificationError, validate_media_url


def _private_tailnet_config(**updates: object) -> StudioSettings:
    values = {
        "_env_file": None,
        "enabled": True,
        "mode": "live",
        "live_enabled": True,
        "origin": "https://node.tail123.ts.net:8451",
        "cookie_secure": True,
        "private_tailnet_enabled": True,
        "public_runtime_enabled": False,
        "preserve_run_artifacts": True,
        "ideation_backend": "codex",
        "FAL_API_KEY": "fal",
        "OPENAI_API_KEY": "openai",
        "session_secret": "s" * 32,
        "daily_budget_usd": 1,
        "run_reservation_usd": 1,
        "render_real": True,
        "asset_retries": 0,
        "cost_profile_version": "private-trial-v1",
        "cost_input_moderation_usd": "0.001",
        "cost_ideation_usd": "0.005",
        "cost_plan_moderation_usd": "0.001",
        "cost_video_request_usd": "0.025",
        "cost_narration_batch_usd": "0.25",
        "cost_render_usd": "0.01",
        "cost_final_moderation_usd": "0.001",
    }
    values.update(updates)
    return StudioSettings(**values)


def test_explicit_private_tailnet_supports_codex_and_retained_live_artifacts() -> None:
    config = _private_tailnet_config()
    assert not config.is_loopback_origin()
    assert config.is_private_origin()
    assert config.live_configuration_errors() == ()
    assert config.public_library_configuration_errors() == ()
    assert config.preserve_run_artifacts


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "https://studio.example.test"},
        {"origin": "https://node.ts.net.attacker.test"},
        {"origin": "http://node.tail123.ts.net:8451"},
        {"cookie_secure": False},
        {"public_runtime_enabled": True},
        {"mode": "fixture"},
    ],
)
def test_private_tailnet_opt_in_rejects_incompatible_exposure(changes: dict) -> None:
    with pytest.raises(ValueError):
        _private_tailnet_config(**changes)


def test_ts_net_hostname_alone_keeps_public_live_requirements() -> None:
    config = _private_tailnet_config(private_tailnet_enabled=False, preserve_run_artifacts=False)
    assert not config.is_private_origin()
    errors = config.live_configuration_errors()
    assert "public_runtime_disabled" in errors
    assert "codex_ideation_requires_loopback_origin" in errors
    with pytest.raises(ValueError, match="private live mode"):
        _private_tailnet_config(private_tailnet_enabled=False)


def test_private_tailnet_keeps_cost_profile_and_admission_checks() -> None:
    assert (
        "cost_profile_missing"
        in _private_tailnet_config(cost_profile_version=None).live_configuration_errors()
    )
    assert (
        "cost_profile_invalid"
        in _private_tailnet_config(cost_video_request_usd=0).live_configuration_errors()
    )
    assert (
        "cost_profile_exceeds_period_budget"
        in _private_tailnet_config(cost_video_request_usd=1).live_configuration_errors()
    )
    assert (
        "admissions_paused"
        in _private_tailnet_config(admission_paused=True).live_configuration_errors()
    )


def test_live_configuration_reports_missing_effect_capabilities_without_rejecting_readiness() -> (
    None
):
    config = StudioSettings(enabled=True, mode="live")

    assert "live_effects_disabled" in config.live_configuration_errors()
    assert "fal_credentials_missing" in config.live_configuration_errors()


def test_public_live_configuration_fails_closed_without_library_or_cost_profile() -> None:
    config = StudioSettings(
        enabled=True,
        mode="live",
        public_runtime_enabled=True,
        live_enabled=True,
        FAL_API_KEY="fal",
        OPENAI_API_KEY="openai",
        session_secret="s" * 32,
        daily_budget_usd=1,
        run_reservation_usd=1,
        render_real=True,
    )

    errors = config.live_configuration_errors()
    assert "library_database_missing" in errors
    assert "library_media_root_missing" in errors
    assert "cost_profile_missing" in errors
    assert "codex_requires_loopback" in errors


def test_public_live_configuration_accepts_complete_openai_profile(tmp_path) -> None:
    config = StudioSettings(
        enabled=True,
        mode="live",
        public_runtime_enabled=True,
        live_enabled=True,
        library_database_url="sqlite+aiosqlite:///separate-library.db",
        library_media_root=tmp_path / "library",
        rights_profile_path=tmp_path / "rights-profile.json",
        ideation_backend="openai",
        FAL_API_KEY="fal",
        OPENAI_API_KEY="openai",
        session_secret="s" * 32,
        daily_budget_usd=100,
        run_reservation_usd=1,
        render_real=True,
        cost_profile_version="owner-approved-v1",
        cost_input_moderation_usd="1",
        cost_ideation_usd="1",
        cost_plan_moderation_usd="1",
        cost_video_request_usd="1",
        cost_narration_batch_usd="1",
        cost_render_usd="1",
        cost_final_moderation_usd="1",
    )

    assert config.cost_profile() is not None
    assert config.live_configuration_errors() == ()


def test_public_live_configuration_rejects_zero_stage_budget(tmp_path) -> None:
    config = StudioSettings(
        enabled=True,
        mode="live",
        public_runtime_enabled=True,
        live_enabled=True,
        library_database_url="sqlite+aiosqlite:///separate-library.db",
        library_media_root=tmp_path / "library",
        rights_profile_path=tmp_path / "rights-profile.json",
        ideation_backend="openai",
        FAL_API_KEY="fal",
        OPENAI_API_KEY="openai",
        session_secret="s" * 32,
        daily_budget_usd=100,
        run_reservation_usd=1,
        render_real=True,
        cost_profile_version="owner-approved-v1",
        cost_input_moderation_usd="1",
        cost_ideation_usd="1",
        cost_plan_moderation_usd="1",
        cost_video_request_usd="1",
        cost_narration_batch_usd="1",
        cost_render_usd="0",
        cost_final_moderation_usd="1",
    )

    assert "cost_profile_invalid" in config.live_configuration_errors()


def test_nonloopback_public_library_requires_explicit_runtime_configuration(tmp_path) -> None:
    config = StudioSettings(
        enabled=False,
        origin="https://studio.example.test",
        cookie_secure=True,
        library_database_url="sqlite+aiosqlite:///separate-library.db",
        library_media_root=tmp_path / "library",
        rights_profile_path=tmp_path / "rights-profile.json",
    )

    assert config.public_library_configuration_errors() == ("public_runtime_disabled",)


def test_public_serving_readiness_does_not_require_live_provider_readiness(tmp_path) -> None:
    config = StudioSettings(
        enabled=False,
        origin="https://studio.example.test",
        cookie_secure=True,
        public_runtime_enabled=True,
        library_database_url="sqlite+aiosqlite:///separate-library.db",
        library_media_root=tmp_path / "library",
        rights_profile_path=tmp_path / "rights-profile.json",
    )

    assert config.public_library_configuration_errors() == ()
    assert config.live_configuration_errors() == ()


def test_nonloopback_live_admission_requires_profile_even_if_public_flag_is_false(tmp_path) -> None:
    config = StudioSettings(
        enabled=True,
        mode="live",
        live_enabled=True,
        origin="https://studio.example.test",
        cookie_secure=True,
        library_database_url="sqlite+aiosqlite:///separate-library.db",
        library_media_root=tmp_path / "library",
        rights_profile_path=tmp_path / "rights-profile.json",
        ideation_backend="openai",
        FAL_API_KEY="fal",
        OPENAI_API_KEY="openai",
        session_secret="s" * 32,
        daily_budget_usd=100,
        run_reservation_usd=1,
        render_real=True,
    )

    errors = config.live_configuration_errors()
    assert "public_runtime_disabled" in errors
    assert "cost_profile_missing" in errors


def test_provider_aliases_are_accepted_without_changing_fixture_defaults() -> None:
    config = StudioSettings.model_validate(
        {"FAL_API_KEY": "fal", "ZERNIO_API_KEY": "zernio", "OPENAI_API_KEY": "openai"}
    )

    assert config.mode == "fixture"
    assert config.fal_key.get_secret_value() == "fal"
    assert config.zernio_key.get_secret_value() == "zernio"
    assert config.openai_key.get_secret_value() == "openai"


def test_knowledge_budget_is_total_deterministic_and_utf8_safe() -> None:
    result = _bounded_knowledge(("ice" * 4000, "diamond" * 4000, "wood" * 4000))

    assert len(result) == 3
    assert sum(len(value.encode("utf-8")) for value in result) <= 6000
    assert result == _bounded_knowledge(("ice" * 4000, "diamond" * 4000, "wood" * 4000))


def test_gcs_media_is_limited_to_the_reviewed_fal_path_prefix() -> None:
    policy = MediaFetchPolicy(
        frozenset({"https://fal.media"}),
        1024,
        2048,
        allowed_path_prefixes=frozenset({"https://storage.googleapis.com/falserverless/"}),
    )

    assert validate_media_url("https://storage.googleapis.com/falserverless/video.mp4", policy)
    with pytest.raises(MediaVerificationError):
        validate_media_url("https://storage.googleapis.com/another-bucket/video.mp4", policy)


def test_default_media_origins_allow_observed_fal_v3b_but_not_a_sibling() -> None:
    settings = StudioSettings()
    policy = MediaFetchPolicy(
        frozenset(settings.media_allowed_origins),
        1024,
        2048,
        allowed_path_prefixes=frozenset(settings.media_allowed_path_prefixes),
    )

    assert validate_media_url("https://v3b.fal.media/audio.wav", policy)
    with pytest.raises(MediaVerificationError):
        validate_media_url("https://v3c.fal.media/audio.wav", policy)
