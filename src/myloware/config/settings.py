"""Application settings using Pydantic."""

import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Project Configuration
    project_id: str = Field(
        default="myloware",
        description="Default project ID for knowledge base setup",
    )

    # Knowledge Base / RAG Configuration
    # Vector store is auto-created with name: project_kb_{project_id}
    # Uses pgvector provider with OpenAI embeddings for fast, reliable RAG

    # Environment
    environment: str = Field(
        default=os.getenv("ENVIRONMENT", "development"),
        description="Deployment environment (development|production)",
    )

    # Llama Stack Configuration
    llama_stack_url: str = "http://localhost:5001"
    llama_stack_model: str = "openai/gpt-5-nano"
    llama_stack_provider: Literal["real", "fake", "off"] = Field(
        default="real",
        description="Llama Stack provider mode: real=call Llama Stack, fake=local stubs, off=disable.",
    )
    use_fake_providers: bool = Field(
        default=False,
        description=(
            "Convenience switch: treat all providers as fake in dev/tests. "
            "This overrides per-provider modes when set to true (off still disables)."
        ),
    )
    content_safety_shield_id: str = Field(
        default="together/meta-llama/Llama-Guard-4-12B",
        description="Default shield id (for inline::llama-guard, this is the model ID)",
    )
    # REMOVED: safety_fail_open - Safety must always fail closed for security.
    # Tests should monkeypatch shields or disable background workflows to avoid external calls.

    # Database Configuration (use psycopg2 driver explicitly)
    database_url: str = "postgresql+psycopg2://myloware:myloware@localhost:5432/myloware"
    db_pool_size: int = Field(default=5, description="Database connection pool size")
    db_max_overflow: int = Field(default=10, description="Database connection pool max overflow")
    async_use_pool: bool = Field(
        default=False,
        description="Use pooled connections for async engine. Disable to force NullPool (safer across event loops).",
    )

    # API Configuration
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_key: str = "dev-api-key"  # Override in production
    media_access_token: str = Field(
        default="",
        description="Optional bearer token for /v1/media endpoints (empty = public).",
    )

    # Public demo (Firebase-hosted UI)
    public_demo_enabled: bool = Field(
        default=False,
        description="Enable unauthenticated public demo endpoints (motivational workflow only).",
    )
    public_demo_allowed_workflows: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["motivational"],
        description="Allowlisted workflows for public demo endpoints.",
    )
    public_demo_token_ttl_hours: int = Field(
        default=72,
        description="TTL (hours) for public demo run tokens.",
    )
    public_demo_rate_limit: str = Field(
        default="10/minute",
        description="Rate limit for public demo start endpoint.",
    )
    public_demo_cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["https://aismr.mjames.dev"],
        description="CORS allowlist for the public demo UI.",
    )

    # Rate limits (SlowAPI syntax). Defaults tuned for dev; override per env.
    run_rate_limit: str = Field(
        default="60/minute",
        description="Rate limit for /v2/runs/start endpoint (SlowAPI syntax)",
    )
    approve_rate_limit: str = Field(
        default="60/minute",
        description="Rate limit for /v1/runs/{id}/approve endpoints",
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: str) -> str:
        """Ensure default API key is not used in production."""
        if os.getenv("ENVIRONMENT") == "production" and v == "dev-api-key":
            raise ValueError("Cannot use default API key in production. Set API_KEY env var.")
        return v

    # External Service API Keys
    brave_api_key: str = Field(
        default="",
        description="Brave Search API key for web search tool",
    )
    openai_api_key: str = ""
    openai_standard_webhook_secret: str = Field(
        default="",
        description="Secret for OpenAI Standard Webhooks (webhook-signature header).",
    )

    remotion_service_url: str = Field(
        default="http://localhost:3001",
        description="Remotion render service URL",
    )
    remotion_webhook_secret: str = Field(
        default="",
        description="Secret for verifying Remotion webhooks",
    )
    openai_sora_signing_secret: str = Field(
        default="",
        description=(
            "Deprecated alias for OPENAI_STANDARD_WEBHOOK_SECRET (OpenAI Standard Webhooks). "
            "Prefer OPENAI_STANDARD_WEBHOOK_SECRET."
        ),
    )

    # Render Provider
    render_provider: str = Field(
        default="local",
        description="Render provider type: 'local' (self-hosted Remotion) or 'lambda' (future)",
    )

    # Retrieval / Milvus
    milvus_uri: str = Field(
        default="",
        description="Milvus endpoint (e.g., localhost:19530). Used by retrieval provider/tooling.",
    )

    upload_post_api_key: str = ""
    upload_post_api_url: str = Field(
        default="https://api.upload-post.com",
        description="Upload-Post API base URL (without /api/upload path)",
    )
    upload_post_poll_interval_s: float = Field(
        default=10.0,
        description="Polling interval (seconds) when Upload-Post returns request_id.",
    )
    upload_post_poll_timeout_s: float = Field(
        default=600.0,
        description="Polling timeout (seconds) for Upload-Post async publishes.",
    )

    # Observability / Alerting
    sentry_dsn: str = Field(
        default="",
        description="Sentry DSN for error monitoring. Leave empty to disable.",
    )

    # Telegram
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_allowed_chat_ids: list[str] = Field(default_factory=list)
    telegram_allow_all_chats: bool = Field(
        default=False,
        description="Allow all chats when telegram_allowed_chat_ids is empty",
    )

    # Webhooks
    webhook_base_url: str = Field(
        default="",
        description="Base URL for webhook callbacks (e.g., https://aismr.mjames.dev)",
    )
    # Sora behavior / provider toggles
    sora_provider: Literal["real", "fake", "off"] = Field(
        default="real",
        description=(
            "Per-provider toggle for OpenAI Sora. "
            "real=call OpenAI, fake=serve local MP4 fixtures via real webhook path, off=disable."
        ),
    )
    sora_fake_clips_dir: str = Field(
        default="fake_clips/sora",
        description="Directory of MP4 fixtures used when SORA_PROVIDER=fake.",
    )
    sora_fake_clip_paths: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Optional explicit MP4 paths for fake Sora provider. " "Env var can be comma-separated."
        ),
    )

    @field_validator("sora_fake_clip_paths", mode="before")
    @classmethod
    def _split_sora_fake_clip_paths(cls, v: Any) -> list[str]:
        if v is None or v == "":
            return []
        if isinstance(v, str):
            if v.lstrip().startswith("["):
                return json.loads(v)
            return [p.strip() for p in v.split(",") if p.strip()]
        return list(v)

    @field_validator("public_demo_allowed_workflows", "public_demo_cors_origins", mode="before")
    @classmethod
    def _split_public_demo_lists(cls, v: Any) -> list[str]:
        if v is None or v == "":
            return []
        if isinstance(v, str):
            if v.lstrip().startswith("["):
                return json.loads(v)
            return [p.strip() for p in v.split(",") if p.strip()]
        return list(v)

    # Remotion provider toggles
    remotion_provider: Literal["real", "fake", "off"] = Field(
        default="real",
        description="Per-provider toggle for Remotion rendering. real=call service, fake=return deterministic stubs, off=disable.",
    )

    # Upload-post provider toggles
    upload_post_provider: Literal["real", "fake", "off"] = Field(
        default="real",
        description="Per-provider toggle for Upload-Post publishing. real=call API, fake=deterministic stubs, off=disable.",
    )

    enable_safety_shields: bool = Field(
        default=True,
        description="Enable content safety shields for write endpoints (always on).",
    )

    # Startup behavior
    fail_fast_on_startup: bool = Field(
        default=True,
        description="Fail startup if critical services (knowledge base, etc.) fail to initialize. "
        "Set to False to allow degraded mode.",
    )

    # Video Cache
    use_video_cache: bool = Field(
        default=False,
        description="Enable video caching - reuse previously generated OpenAI Sora videos",
    )
    cache_new_videos: bool = Field(
        default=True,
        description="Store newly generated videos in cache (requires topic/sign metadata)",
    )

    # Knowledge Base Tuning
    kb_max_file_bytes: int = Field(
        default=512_000,  # 500 KB
        description="Skip knowledge docs larger than this size (bytes)",
    )
    kb_max_total_bytes: int = Field(
        default=50_000_000,  # ~50 MB
        description="Total KB payload cap; extra files are skipped with warning",
    )
    kb_max_docs: int = Field(
        default=500,
        description="Maximum number of documents to ingest per startup",
    )
    kb_parallel_reads: int = Field(
        default=8,
        description="Thread pool size for reading knowledge docs in parallel",
    )
    kb_upload_batch_size: int = Field(
        default=100,
        description="Chunk size for batching KB uploads to the vector store",
    )
    kb_skip_ingest_on_start: bool = Field(
        default=False,
        description="If true, skip KB ingestion at startup (can reload via admin endpoint)",
    )
    kb_chunk_max_chars: int = Field(
        default=2048,  # 512 tokens * 4 chars/token (approximate)
        description="Maximum characters per KB chunk before splitting (target: 512 tokens)",
    )
    kb_chunk_overlap_chars: int = Field(
        default=400,  # 100 tokens * 4 chars/token (approximate)
        description="Character overlap when splitting long KB documents (target: 100 tokens)",
    )

    # Transcode hardening
    transcode_max_concurrency: int = Field(
        default=2, description="Max concurrent ffmpeg transcodes to avoid resource exhaustion."
    )
    transcode_allow_file_urls: bool = Field(
        default=False,
        description="Allow file:// URLs for transcode inputs (local-only; disable in prod).",
    )
    transcode_allow_private: bool = Field(
        default=False,
        description="Allow transcoding of URLs that resolve to private/local addresses. Defaults to false for SSRF safety.",
    )
    transcode_allowed_domains: list[str] = Field(
        default_factory=list,
        description="Optional allowlist of hostnames for transcoding; empty means any public host.",
    )
    transcode_storage_backend: Literal["local", "s3"] = Field(
        default="local",
        description=(
            "Where transcoded clips are stored. "
            "local=write to filesystem (must be shared between API and workers), "
            "s3=upload to S3 and store s3:// URIs (recommended for multi-replica)."
        ),
    )
    transcode_output_dir: str = Field(
        default=str(Path(tempfile.gettempdir()) / "myloware_videos"),
        description="Filesystem output dir for transcoded clips when transcode_storage_backend=local.",
    )
    transcode_s3_bucket: str = Field(
        default="",
        description="S3 bucket for transcoded clips when transcode_storage_backend=s3.",
    )
    transcode_s3_prefix: str = Field(
        default="myloware/transcoded",
        description="Key prefix for transcoded clips within the S3 bucket.",
    )
    transcode_s3_endpoint_url: str = Field(
        default="",
        description="Optional S3 endpoint URL for S3-compatible stores (e.g., R2/MinIO).",
    )
    transcode_s3_region: str = Field(
        default="",
        description="AWS region for the S3 client (leave empty if not required by your endpoint).",
    )
    transcode_s3_presign_seconds: int = Field(
        default=86400,
        description="Presigned GET URL TTL used when resolving s3:// clip URIs for Remotion.",
    )

    # Budget / cost guards
    max_runs_last_24h: int = Field(
        default=1000,
        description="Maximum number of runs allowed in the past 24 hours (simple budget guard).",
    )
    daily_cost_budget_usd: float = Field(
        default=200.0,
        description="Daily cost budget for guard checks (rough, per-run estimate).",
    )
    estimated_cost_per_run_usd: float = Field(
        default=0.5,
        description="Estimated cost per run used for budget guard.",
    )

    # Remotion / Render security
    remotion_api_secret: str = Field(
        default="",
        description="Shared secret for signing render requests to the Remotion service",
    )
    remotion_allow_composition_code: bool = Field(
        default=False,
        description="Allow custom composition_code payloads (only enable when sandboxed)",
    )
    remotion_sandbox_enabled: bool = Field(
        default=False,
        description="Indicates render service runs in an isolated sandbox (container/VM with no internal network).",
    )
    remotion_sandbox_strict: bool = Field(
        default=False,
        description="Require strict sandbox enforcement before allowing composition_code.",
    )
    disable_background_workflows: bool = Field(
        default=False,
        description="If true, skip enqueueing background workflow execution (useful for fast tests).",
    )

    # Workflow dispatch / scaling
    workflow_dispatcher: Literal["inprocess", "db"] = Field(
        default="inprocess",
        description=(
            "How the API dispatches workflow work. "
            "inprocess=FastAPI BackgroundTasks (single-process dev), "
            "db=enqueue durable jobs to Postgres for worker processes."
        ),
    )
    worker_id: str = Field(
        default="",
        description="Optional worker identifier used for job claiming. If empty, workers auto-generate.",
    )
    job_poll_interval_seconds: float = Field(
        default=1.0,
        description="Worker poll interval when no jobs are available.",
    )
    job_lease_seconds: float = Field(
        default=600.0,
        description="Job lease duration in seconds (workers should finish before expiry or renew).",
    )
    job_max_attempts: int = Field(
        default=5,
        description="Default maximum retry attempts for queued jobs.",
    )
    job_retry_delay_seconds: float = Field(
        default=5.0,
        description="Base retry delay (seconds) for failed jobs (worker may apply backoff).",
    )
    worker_concurrency: int = Field(
        default=4,
        description="Max concurrent jobs per worker process.",
    )

    skip_run_visibility_check: bool = Field(
        default=False,
        description="Skip verifying run visibility after commit (test helper for fake repos).",
    )

    # LangGraph Configuration
    use_langgraph_engine: bool = Field(
        default=True,
        description="Use LangGraph for workflow orchestration.",
    )

    # Circuit Breaker Configuration
    circuit_breaker_enabled: bool = Field(
        default=True,
        description="Enable circuit breaker for Llama Stack client calls.",
    )
    circuit_breaker_failure_threshold: int = Field(
        default=5,
        description="Number of failures before opening circuit.",
    )
    circuit_breaker_recovery_timeout: float = Field(
        default=30.0,
        description="Seconds to wait before attempting recovery (half-open state).",
    )

    @model_validator(mode="after")
    def validate_transcode_storage(self) -> "Settings":
        if self.transcode_storage_backend == "s3" and not self.transcode_s3_bucket:
            raise ValueError(
                "TRANSCODE_S3_BUCKET is required when TRANSCODE_STORAGE_BACKEND=s3. "
                "Either set a bucket or use TRANSCODE_STORAGE_BACKEND=local."
            )
        return self

    @model_validator(mode="after")
    def validate_transcode_backend_for_production(self) -> "Settings":
        """Force shared transcoded storage when production uses real providers."""
        sora_mode = (
            "fake"
            if (self.use_fake_providers and self.sora_provider == "real")
            else self.sora_provider
        )
        remotion_mode = (
            "fake"
            if (self.use_fake_providers and self.remotion_provider == "real")
            else self.remotion_provider
        )
        if (
            self.environment == "production"
            and (sora_mode == "real" or remotion_mode == "real")
            and bool(self.webhook_base_url)
            and self.transcode_storage_backend != "s3"
        ):
            raise ValueError(
                "TRANSCODE_STORAGE_BACKEND=s3 is required in production when SORA_PROVIDER=real "
                "or REMOTION_PROVIDER=real, so transcoded clips are shared across replicas."
            )
        return self

    @model_validator(mode="after")
    def validate_webhook_url_for_production(self) -> "Settings":
        """Ensure webhook_base_url is set when required by real providers."""
        sora_mode = (
            "fake"
            if (self.use_fake_providers and self.sora_provider == "real")
            else self.sora_provider
        )
        remotion_mode = (
            "fake"
            if (self.use_fake_providers and self.remotion_provider == "real")
            else self.remotion_provider
        )
        if (
            self.environment == "production"
            and (sora_mode == "real" or remotion_mode == "real")
            and not self.webhook_base_url
        ):
            raise ValueError(
                "WEBHOOK_BASE_URL is required when SORA_PROVIDER=real or REMOTION_PROVIDER=real. "
                "Set it to your public API URL (e.g., https://aismr.mjames.dev)"
            )
        return self

    @model_validator(mode="after")
    def validate_model(self) -> "Settings":
        """Ensure a model is configured."""
        if not self.llama_stack_model:
            raise ValueError("LLAMA_STACK_MODEL must be configured")
        return self

    @field_validator("enable_safety_shields", mode="before")
    @classmethod
    def force_safety_on(cls, _v: bool) -> bool:
        """Safety shields are always enabled."""
        return True

    # REMOVED: safety_fail_open validator - Safety must always fail closed.

    @field_validator("content_safety_shield_id", mode="before")
    @classmethod
    def force_content_shield(cls, _v: str) -> str:
        """Use the model ID for inline::llama-guard shields."""
        if not _v:
            return "together/meta-llama/Llama-Guard-4-12B"
        return _v

    @field_validator("remotion_allow_composition_code", mode="before")
    @classmethod
    def gate_composition_code(cls, v: bool, info: Any) -> bool:
        """Only allow composition_code when sandboxed or fake providers."""
        data = getattr(info, "data", {}) or {}
        remotion_provider = data.get("remotion_provider", "real")
        sandbox = data.get("remotion_sandbox_enabled", False)
        sandbox_strict = data.get("remotion_sandbox_strict", False)
        if data.get("use_fake_providers") and remotion_provider == "real":
            remotion_provider = "fake"
        return bool(v and ((sandbox and sandbox_strict) or remotion_provider in {"fake", "off"}))

    # Observability
    log_webhook_payloads: bool = Field(
        default=False,
        description="If true, log raw webhook payloads (may contain sensitive data).",
    )
    log_level: str = "INFO"
    enable_telemetry: bool = True


# Global settings instance
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Lazily construct Settings so tests and CLIs can set env vars before first access.
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


class _SettingsProxy:
    """Lazy proxy for Settings.

    This avoids eager settings instantiation at import time, which can make tests
    order-dependent when env vars are changed during `pytest_configure()`.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_settings(), name)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SettingsProxy {get_settings()!r}>"


settings = _SettingsProxy()
