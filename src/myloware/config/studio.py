"""Explicit configuration for the visitor-operated monthly studio."""

from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from myloware.studio.budget import CostConfigurationError, StudioCostProfile


class StudioSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AISMR_", env_file=".env", extra="ignore")

    enabled: bool = False
    mode: Literal["fixture", "recorded", "planning", "local", "live"] = "fixture"
    # Enabling the routes is intentionally separate from enabling paid effects.
    live_enabled: bool = False
    fixture_moderation: Literal["allow", "deny", "malformed", "outage"] = "allow"
    origin: str = "http://127.0.0.1:8311"
    session_secret: SecretStr = SecretStr("local-fixture-session-secret-only")
    cookie_secure: bool = False
    session_cookie_suffix: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    session_hours: int = Field(default=24, ge=1, le=168)
    visitor_runs_24h: int = Field(default=1, ge=1, le=20)
    ip_runs_24h: int = Field(default=4, ge=1, le=100)
    active_runs: int = Field(default=2, ge=1, le=20)
    # Private fixture walkthroughs may opt out of admission quotas without
    # changing durable history or any live-effect boundary.
    fixture_unlimited_admissions: bool = False
    daily_budget_usd: Decimal = Field(default=Decimal(0), ge=0, decimal_places=6)
    run_reservation_usd: Decimal = Field(default=Decimal(0), ge=0, decimal_places=6)
    # The legacy flat reservation remains for non-public local live operation.
    # Public runtime uses this versioned profile to reserve every selected stage.
    cost_profile_version: str | None = None
    cost_input_moderation_usd: Decimal | None = Field(default=None, ge=0, decimal_places=6)
    cost_ideation_usd: Decimal | None = Field(default=None, ge=0, decimal_places=6)
    cost_plan_moderation_usd: Decimal | None = Field(default=None, ge=0, decimal_places=6)
    cost_video_request_usd: Decimal | None = Field(default=None, ge=0, decimal_places=6)
    cost_narration_batch_usd: Decimal | None = Field(default=None, ge=0, decimal_places=6)
    cost_render_usd: Decimal | None = Field(default=None, ge=0, decimal_places=6)
    cost_final_moderation_usd: Decimal | None = Field(default=None, ge=0, decimal_places=6)
    admission_paused: bool = False
    public_runtime_enabled: bool = False
    # Explicit operator declaration for Tailscale Serve behind tailnet access
    # controls. A ts.net hostname alone does not establish private exposure.
    private_tailnet_enabled: bool = False
    library_database_url: str | None = None
    library_media_root: Path | None = None
    rights_profile_path: Path | None = None
    plan_review_hours: int = Field(default=24, ge=1, le=168)
    final_review_hours: int = Field(default=24, ge=1, le=168)
    unknown_hold_reconcile_hours: int = Field(default=48, ge=1, le=720)
    asset_retries: int = Field(default=1, ge=0, le=2)
    plan_revisions: int = Field(default=1, ge=0, le=2)
    poll_seconds: float = Field(default=3.0, ge=0.1, le=60)
    job_concurrency: int = Field(default=2, ge=1, le=16)
    run_deadline_hours: int = Field(default=24, ge=1, le=48)
    retention_days: int = Field(default=7, ge=1, le=90)
    # A first private live trial may retain its local evidence after review.
    # This is deliberately unavailable to fixture, recorded, local, and public
    # runtimes, whose established cleanup and projection semantics stay intact.
    preserve_run_artifacts: bool = False
    media_root: Path = Path(".local/aismr/media")
    fixture_root: Path = Path(".local/aismr/fixtures")
    recorded_root: Path | None = None
    # Hosted recorded walkthrough: immutable R2 assets, no provider or renderer.
    recorded_final_reuse: bool = False
    recorded_bundle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recorded_bucket: str | None = None
    local_media_root: Path | None = None
    media_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "https://fal.media",
            "https://v3.fal.media",
            "https://v3b.fal.media",
        ]
    )
    media_allowed_path_prefixes: list[str] = Field(
        default_factory=lambda: ["https://storage.googleapis.com/falserverless/"]
    )
    max_asset_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    max_run_bytes: int = Field(default=256 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    fal_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("AISMR_FAL_KEY", "FAL_API_KEY", "FAL_KEY"),
    )
    zernio_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("AISMR_ZERNIO_KEY", "ZERNIO_API_KEY", "ZERNIO_KEY"),
    )
    openai_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("AISMR_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    ideation_backend: Literal["codex", "openai"] = "codex"
    # Live operators opt into the new call inventory; fixture demos can exercise
    # revisions for free. Existing live configurations retain their old profile.
    planner_version: Literal["single-v1", "creative-v2"] = "single-v1"
    codex_path: Path = Path("/opt/homebrew/bin/codex")
    codex_model: str = "gpt-5.6-luna"
    codex_reasoning_effort: Literal["low"] = "low"
    ideation_deadline_seconds: int = Field(default=120, ge=1, le=120)
    creative_workflow_deadline_seconds: int = Field(default=660, ge=1, le=660)
    creative_repair_attempts: int = Field(default=0, ge=0, le=2)
    openai_ideation_model: str = "gpt-4o-mini"
    tiktok_account_id: str = ""
    tiktok_account_name: str = "AISMR demo account"
    tiktok_privacy: Literal["PUBLIC_TO_EVERYONE", "SELF_ONLY"] = "PUBLIC_TO_EVERYONE"
    render_real: bool = False
    music_id: str | None = "tender-moment"

    def planning_calls(self) -> tuple[tuple[int, str], ...]:
        """Reserve the bounded worst case, including each permitted user revision."""
        if self.planner_version != "creative-v2" or self.mode == "recorded":
            return ()
        return tuple(
            (revision, role)
            for revision in range(1, self.plan_revisions + 2)
            for role in (
                "explore_object",
                "explore_surreal",
                "curate",
                "write_shots",
                "replenish",
                "recurate",
            )
            + tuple(f"repair_{attempt}" for attempt in range(1, self.creative_repair_attempts + 1))
        )

    @model_validator(mode="after")
    def validate_effect_boundary(self) -> "StudioSettings":
        if self.mode == "fixture" and "planner_version" not in self.model_fields_set:
            self.planner_version = "creative-v2"
        if self.fixture_unlimited_admissions and self.mode != "fixture":
            raise ValueError("Fixture unlimited admissions require fixture mode")
        origin = urlsplit(self.origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
            or origin.username
        ):
            raise ValueError("AISMR_ORIGIN must be a single HTTP(S) origin")
        loopback = origin.hostname in {"localhost", "127.0.0.1", "::1"}
        if not loopback and (origin.scheme != "https" or not self.cookie_secure):
            raise ValueError("Non-loopback AISMR requires HTTPS and secure cookies")
        if self.private_tailnet_enabled and (
            self.mode != "live"
            or self.public_runtime_enabled
            or origin.scheme != "https"
            or not self.cookie_secure
            or not origin.hostname.casefold().endswith(".ts.net")
        ):
            raise ValueError(
                "Private tailnet requires HTTPS live mode with public runtime disabled"
            )
        if self.run_reservation_usd > self.daily_budget_usd:
            raise ValueError("Per-run reservation exceeds daily budget")
        if self.mode == "recorded" and self.recorded_root is None:
            raise ValueError("Recorded mode requires the approved media archive root")
        if self.recorded_final_reuse:
            if self.mode != "recorded" or self.live_enabled or self.render_real:
                raise ValueError("Recorded final reuse requires recorded mode with live/render off")
            if any(
                key.get_secret_value() for key in (self.openai_key, self.fal_key, self.zernio_key)
            ):
                raise ValueError("Recorded final reuse forbids generation and posting credentials")
            if not self.recorded_bundle_sha256 or not self.recorded_bucket:
                raise ValueError("Recorded final reuse requires a pinned bundle and R2 bucket")
            if len(
                self.session_secret.get_secret_value()
            ) < 32 or self.session_secret.get_secret_value().startswith("local-fixture"):
                raise ValueError("Recorded final reuse requires a private session secret")
        if self.mode in {"planning", "local"} and not self.enabled:
            raise ValueError(f"{self.mode.title()} mode requires AISMR_ENABLED")
        if self.preserve_run_artifacts and (
            self.mode != "live" or not self.is_private_origin() or self.public_runtime_enabled
        ):
            raise ValueError("Preserving run artifacts requires private live mode")
        return self

    def planning_configuration_errors(self) -> tuple[str, ...]:
        """Return effect-free readiness failures for private text planning only."""
        if self.mode != "planning":
            return ()
        errors: list[str] = []
        if not self.enabled:
            errors.append("studio_disabled")
        if self.live_enabled:
            errors.append("planning_live_effects_must_be_disabled")
        if self.planner_version != "creative-v2":
            errors.append("planning_requires_creative_v2")
        if self.ideation_backend != "codex":
            errors.append("planning_requires_codex")
        if not self.openai_key.get_secret_value():
            errors.append("openai_credentials_missing")
        secret = self.session_secret.get_secret_value()
        if len(secret) < 32 or secret.startswith("local-fixture"):
            errors.append("planning_session_secret_invalid")
        origin = urlsplit(self.origin)
        allowed_private_origin = self.is_loopback_origin() or (
            origin.scheme == "https"
            and self.cookie_secure
            and bool(origin.hostname)
            and origin.hostname.casefold().endswith(".ts.net")
        )
        if not allowed_private_origin:
            errors.append("planning_private_origin_required")
        return tuple(errors)

    def local_configuration_errors(self) -> tuple[str, ...]:
        """Return private rehearsal readiness failures without media-provider effects."""
        if self.mode != "local":
            return ()
        errors: list[str] = []
        if not self.enabled:
            errors.append("studio_disabled")
        if self.live_enabled:
            errors.append("local_live_effects_must_be_disabled")
        if self.public_runtime_enabled:
            errors.append("local_public_runtime_must_be_disabled")
        if self.planner_version != "creative-v2":
            errors.append("local_requires_creative_v2")
        if self.ideation_backend != "codex":
            errors.append("local_requires_codex")
        if not self.openai_key.get_secret_value():
            errors.append("openai_credentials_missing")
        if self.fal_key.get_secret_value():
            errors.append("local_fal_credentials_forbidden")
        if self.local_media_root is None:
            errors.append("local_media_archive_missing")
        if not self.render_real:
            errors.append("real_renderer_disabled")
        secret = self.session_secret.get_secret_value()
        if len(secret) < 32 or secret.startswith("local-fixture"):
            errors.append("local_session_secret_invalid")
        origin = urlsplit(self.origin)
        private_origin = self.is_loopback_origin() or (
            origin.scheme == "https"
            and self.cookie_secure
            and bool(origin.hostname)
            and origin.hostname.casefold().endswith(".ts.net")
        )
        if not private_origin:
            errors.append("local_private_origin_required")
        return tuple(errors)

    def live_configuration_errors(self) -> tuple[str, ...]:
        """Return local, effect-free readiness failures without contacting providers."""
        if self.mode != "live":
            return ()
        errors: list[str] = []
        if not self.live_enabled:
            errors.append("live_effects_disabled")
        if not self.fal_key.get_secret_value():
            errors.append("fal_credentials_missing")
        if self.daily_budget_usd <= 0 or self.run_reservation_usd <= 0:
            errors.append("live_budget_missing")
        secret = self.session_secret.get_secret_value()
        if len(secret) < 32 or secret.startswith("local-fixture"):
            errors.append("live_session_secret_invalid")
        if not self.render_real:
            errors.append("real_renderer_disabled")
        if not self.openai_key.get_secret_value():
            errors.append("openai_credentials_missing")
        if self.ideation_backend == "codex" and not self.is_private_origin():
            errors.append("codex_ideation_requires_loopback_origin")
        public_runtime = self.public_runtime_enabled or not self.is_private_origin()
        if public_runtime:
            errors.extend(self.public_library_configuration_errors())
            if self.ideation_backend == "codex":
                errors.append("codex_requires_loopback")
        if public_runtime or self.private_tailnet_enabled:
            if self.admission_paused:
                errors.append("admissions_paused")
            if self.live_enabled:
                profile = self.cost_profile()
                if profile is None:
                    errors.append("cost_profile_missing")
                else:
                    try:
                        reserve = profile.reserve_with_revisions(
                            self.plan_revisions,
                            planning_calls=self.planning_calls(),
                            asset_retries=self.asset_retries,
                        )
                        if reserve > self.daily_budget_usd:
                            errors.append("cost_profile_exceeds_period_budget")
                    except CostConfigurationError:
                        errors.append("cost_profile_invalid")
        return tuple(errors)

    def is_private_origin(self) -> bool:
        """Recognize loopback or explicitly configured private Tailscale Serve."""
        origin = urlsplit(self.origin)
        return self.is_loopback_origin() or (
            self.private_tailnet_enabled
            and self.mode == "live"
            and not self.public_runtime_enabled
            and origin.scheme == "https"
            and self.cookie_secure
            and bool(origin.hostname)
            and origin.hostname.casefold().endswith(".ts.net")
        )

    def is_loopback_origin(self) -> bool:
        """Return whether the validated Studio origin is limited to this host."""
        return urlsplit(self.origin).hostname in {"localhost", "127.0.0.1", "::1"}

    @property
    def session_cookie_name(self) -> str:
        """Keep independent Studio instances from replacing sessions.

        An explicit suffix isolates studios sharing a host. Without a suffix,
        canonical local and public origins retain the established name. Alternate
        loopback ports receive their own name; cookies are scoped to host, not port.
        """
        if self.session_cookie_suffix is not None:
            return f"aismr_session_{self.session_cookie_suffix}"
        origin = urlsplit(self.origin)
        if not self.is_loopback_origin() or origin.port in {None, 8311}:
            return "aismr_session"
        return f"aismr_session_{origin.port}"

    def public_library_configuration_errors(self) -> tuple[str, ...]:
        """Return public-serving prerequisites without requiring paid providers.

        A non-loopback origin is public unless explicitly configured for private
        Tailscale Serve. The route/runtime composition layer performs the remaining
        source-root separation checks before it opens the library.
        """
        if self.is_private_origin() and not self.public_runtime_enabled:
            return ()
        errors: list[str] = []
        if not self.public_runtime_enabled:
            errors.append("public_runtime_disabled")
        if not self.library_database_url:
            errors.append("library_database_missing")
        if self.library_media_root is None:
            errors.append("library_media_root_missing")
        if self.rights_profile_path is None:
            errors.append("rights_profile_missing")
        return tuple(errors)

    def cost_profile(self) -> StudioCostProfile | None:
        """Build a complete configured profile without selecting provider prices."""
        values = (
            self.cost_input_moderation_usd,
            self.cost_ideation_usd,
            self.cost_plan_moderation_usd,
            self.cost_video_request_usd,
            self.cost_narration_batch_usd,
            self.cost_render_usd,
            self.cost_final_moderation_usd,
        )
        if not self.cost_profile_version or any(value is None for value in values):
            return None
        return StudioCostProfile(
            version=self.cost_profile_version,
            input_moderation=self.cost_input_moderation_usd,
            ideation=self.cost_ideation_usd,
            plan_moderation=self.cost_plan_moderation_usd,
            video_request=self.cost_video_request_usd,
            narration_batch=self.cost_narration_batch_usd,
            render=self.cost_render_usd,
            final_moderation=self.cost_final_moderation_usd,
        )


def get_studio_settings() -> StudioSettings:
    """Read explicit settings at composition boundaries; tests may inject an instance."""
    return StudioSettings()
