"""FastAPI application for AISMR API."""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path as _Path
from typing import Any, AsyncGenerator, Awaitable, Callable
from uuid import uuid4

import sentry_sdk
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles as _StaticFiles
from prometheus_client import Counter, Histogram
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from myloware.api import routes
from myloware.api.dependencies import api_key_header, verify_api_key
from myloware.api.errors import DomainError, to_http_exception
from myloware.api.middleware.safety import safety_shield_middleware
from myloware.api.rate_limit import key_api_key_or_ip
from myloware.api.routes import admin, chat, langgraph, media, public_demo, runs, telegram, webhooks
from myloware.api.routes import metrics as metrics_route
from myloware.api.routes.studio import router as studio_router
from myloware.api.routes.studio_internal import router as studio_internal_router
from myloware.app_version import get_app_version
from myloware.config import settings
from myloware.config.provider_modes import effective_llama_stack_provider
from myloware.knowledge.loader import (
    load_documents_with_manifest,
    load_manifest,
    save_manifest,
)
from myloware.knowledge.setup import get_existing_vector_store, setup_project_knowledge
from myloware.llama_clients import get_sync_client
from myloware.observability.logging import logger, request_id_var
from myloware.storage.database import init_async_db, init_db

limiter = Limiter(key_func=key_api_key_or_ip)


def _should_init_sentry() -> bool:
    """Guard Sentry initialization in tests/dev to avoid noisy pending-event logs."""
    if not settings.sentry_dsn:
        return False
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    if os.getenv("DISABLE_SENTRY", "").lower() in ("1", "true", "yes"):
        return False
    return True


REQUEST_COUNT = Counter(
    "myloware_http_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
REQUEST_LATENCY = Histogram(
    "myloware_http_request_duration_seconds",
    "HTTP request duration in seconds",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
    labelnames=["path"],
)


def _metrics_path(request: Request) -> str:
    """Return a low-cardinality path label for metrics."""
    route = request.scope.get("route")
    if route is not None:
        path = getattr(route, "path", None)
        if path:
            return str(path)
    return "unmatched"


async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    retry_after_header = exc.headers.get("Retry-After") if exc.headers else None
    retry_after = retry_after_header or "60"
    logger.warning(
        "rate_limit_exceeded",
        path=str(request.url.path),
        method=request.method,
        limit=exc.detail,
        request_id=request.headers.get("X-Request-ID"),
    )
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limited",
            "detail": f"Too many requests. Retry after {retry_after} seconds.",
            "retry_after_seconds": int(retry_after) if str(retry_after).isdigit() else retry_after,
            "limit": exc.detail,
        },
        headers=exc.headers or {"Retry-After": str(retry_after)},
    )


def _load_knowledge_documents(project_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load knowledge docs with filters, hashing, and manifest tracking."""

    prev_manifest = load_manifest()
    docs, manifest = load_documents_with_manifest(
        project_id, include_global=True, read_content=True
    )
    manifest["unchanged"] = bool(
        prev_manifest and prev_manifest.get("hash") == manifest.get("hash")
    )
    save_manifest(manifest)

    doc_dicts = [
        {
            "id": d.id,
            "content": d.content,
            "metadata": {
                **d.metadata,
                "filename": d.filename,
                "type": "knowledge",
            },
        }
        for d in docs
    ]

    return doc_dicts, manifest


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan - setup knowledge base and LangGraph checkpointer on startup."""
    from myloware.observability import init_observability

    init_observability()
    logger.info("Starting AISMR API...")

    # LangGraph engine lifecycle (explicit; stored on app.state for handlers).
    if settings.use_langgraph_engine:
        from myloware.workflows.langgraph.graph import LangGraphEngine, set_langgraph_engine

        engine = LangGraphEngine()
        app.state.langgraph_engine = engine
        # Back-compat: make this engine the process default so background tasks
        # and non-request code paths share the same checkpointer lifecycle.
        set_langgraph_engine(engine)

    # Optional Sentry instrumentation
    if _should_init_sentry():
        try:
            sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1, shutdown_timeout=0)
        except Exception as exc:  # tolerates invalid/empty DSN in dev/test
            logger.warning("Sentry initialization skipped: %s", exc)

    # Note: Tracing is handled by Llama Stack's native telemetry
    # We use client.telemetry.log_event() for custom events

    # Track degraded state for health checks
    app.state.knowledge_base_healthy = False
    app.state.knowledge_base_error = None
    app.state.remotion_sandbox_enabled = settings.remotion_sandbox_enabled

    # Ensure database tables exist (supports async + sync drivers)
    try:
        if "aiosqlite" in settings.database_url or "asyncpg" in settings.database_url:
            await init_async_db()
        else:
            init_db()
        app.state.database_ready = True
    except Exception as exc:
        logger.error("Database initialization failed: %s", exc)
        app.state.database_ready = False
        if settings.fail_fast_on_startup:
            raise

    # The public library has its own database. Bootstrap it once here, never
    # from a read route; absent local configuration leaves gallery unavailable.
    try:
        from myloware.config.studio import get_studio_settings
        from myloware.storage.database import get_async_session_factory
        from myloware.storage.studio_store import StudioStore
        from myloware.studio.portfolio_runtime import open_portfolio_library

        studio_store = StudioStore(get_async_session_factory(), get_studio_settings())
        async with open_portfolio_library(studio_store) as library:
            await library.initialize()
        app.state.portfolio_library_ready = True
    except Exception as exc:
        app.state.portfolio_library_ready = False
        logger.info("Portfolio library startup skipped or unavailable: %s", exc)

    # Initialize the durable saver for file SQLite and PostgreSQL. Explicit
    # :memory: test databases remain ephemeral.
    if settings.use_langgraph_engine:
        try:
            engine = getattr(app.state, "langgraph_engine", None)
            if engine is None:
                raise RuntimeError("LangGraph engine missing from app.state")
            await engine.ensure_checkpointer_initialized()
            logger.info("LangGraph async checkpointer initialized")
        except Exception as exc:
            logger.error("Failed to initialize LangGraph checkpointer: %s", exc)
            if settings.fail_fast_on_startup:
                raise

    llama_mode = effective_llama_stack_provider(settings)

    # Skip knowledge base setup when Llama Stack is not real (tests/dev stubs).
    if llama_mode != "real":
        logger.info("Skipping knowledge base setup (LLAMA_STACK_PROVIDER=%s)", llama_mode)
        app.state.vector_db_id = f"project_kb_{settings.project_id}"
        app.state.knowledge_base_healthy = True  # Expected in dev mode
        app.state.shields_available = True  # assume dev shields mocked
    else:
        # Setup knowledge base - always auto-discover/create by name
        try:
            client = get_sync_client()
            project_id = settings.project_id
            documents, manifest = _load_knowledge_documents(project_id)
            logger.info(
                "KB scan complete: files=%d, docs_loaded=%d, hash=%s, unchanged=%s",
                len(manifest.get("files", [])),
                len(documents),
                manifest.get("hash"),
                manifest.get("unchanged"),
            )

            store_name = f"project_kb_{project_id}"
            existing_store = get_existing_vector_store(client, store_name)

            if settings.kb_skip_ingest_on_start:
                app.state.vector_db_id = existing_store or store_name
                app.state.knowledge_base_healthy = False
                app.state.knowledge_base_error = "KB ingestion skipped by KB_SKIP_INGEST_ON_START"
                logger.warning(
                    "KB ingestion skipped on start; vector_db_id=%s", app.state.vector_db_id
                )
            else:
                # If unchanged and store exists, skip ingest
                if manifest.get("unchanged") and existing_store:
                    vector_db_id = existing_store
                    logger.info("KB unchanged; reusing existing vector store %s", vector_db_id)
                else:
                    vector_db_id = setup_project_knowledge(
                        client, project_id, documents=documents if documents else None
                    )

                app.state.vector_db_id = vector_db_id
                app.state.knowledge_base_healthy = True
                logger.info("Knowledge base ready: %s (project: %s)", vector_db_id, project_id)

            # Register RAG toolgroup (required for file_search tools)
            try:
                client.toolgroups.register(
                    toolgroup_id="builtin::rag",
                    provider_id="rag-runtime",
                )
                logger.info("Registered RAG toolgroup (rag-runtime)")
            except Exception as rag_exc:
                # RAG is a hard dependency in real environments; fail fast so we don't
                # start in a degraded state with missing file_search tooling.
                error_msg = f"Failed to register RAG toolgroup: {rag_exc}"
                logger.error(error_msg)
                raise RuntimeError(error_msg) from rag_exc

            # Register web search tool if Brave API key is configured
            if settings.brave_api_key:
                try:
                    client.toolgroups.register(
                        toolgroup_id="builtin::websearch",
                        provider_id="brave-search",
                        args={"max_results": 5},
                    )
                    logger.info("Registered web search toolgroup (brave-search)")
                except Exception as tool_exc:
                    logger.warning("Failed to register web search toolgroup: %s", tool_exc)
            else:
                logger.info("Skipping web search toolgroup (BRAVE_API_KEY not set)")

            # Safety shields are registered in llama_stack/run-milvus.yaml
            # Just verify they're available
            try:
                shields = client.shields.list()
                if hasattr(shields, "data"):
                    shield_list = shields.data
                elif isinstance(shields, list):
                    shield_list = shields
                else:
                    shield_list = []
                expected_shield_id = settings.content_safety_shield_id
                content_safety_exists = any(s.identifier == expected_shield_id for s in shield_list)
                if content_safety_exists:
                    logger.info(
                        "Safety shield available: %s (registered in YAML)", expected_shield_id
                    )
                    app.state.shields_available = True
                else:
                    logger.warning("Safety shield not found: %s", expected_shield_id)
                    app.state.shields_available = False
            except Exception as shield_exc:
                logger.warning("Failed to check safety shields: %s", shield_exc)
                app.state.shields_available = False

        except Exception as exc:
            error_msg = f"Knowledge base setup failed: {exc}"
            logger.error(error_msg)
            app.state.vector_db_id = f"project_kb_{settings.project_id}"
            app.state.knowledge_base_error = error_msg

            # RAG is mandatory outside of fake-provider mode; never continue degraded.
            raise RuntimeError(error_msg) from exc

    yield

    logger.info("Shutting down AISMR API...")

    # Cleanup LangGraph async checkpointer
    if settings.use_langgraph_engine:
        try:
            from myloware.workflows.langgraph.graph import set_langgraph_engine

            engine = getattr(app.state, "langgraph_engine", None)
            if engine is not None:
                await engine.shutdown()
            set_langgraph_engine(None)
            logger.info("LangGraph async checkpointer closed")
        except Exception as exc:
            logger.warning("Error closing LangGraph checkpointer: %s", exc)


app = FastAPI(
    title="AISMR API",
    description="AISMR: a LangGraph workflow for reviewed surreal video creation",
    version=get_app_version(),
    lifespan=lifespan,
)

# Optional CORS for public demo UI
if settings.public_demo_enabled and settings.public_demo_cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.public_demo_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

app.middleware("http")(safety_shield_middleware)


@app.middleware("http")
async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Middleware to manage X-Request-ID header and contextvar propagation."""

    incoming_request_id = request.headers.get("X-Request-ID")
    request_id = incoming_request_id or str(uuid4())

    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)

    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def timing_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    try:
        path_label = _metrics_path(request)
        REQUEST_COUNT.labels(
            method=request.method,
            path=path_label,
            status=response.status_code,
        ).inc()
        REQUEST_LATENCY.labels(path=path_label).observe(duration_ms / 1000.0)
    # Metrics must not fail an otherwise valid request.
    except Exception as metrics_exc:  # noqa: BLE001
        logger.debug(
            "metrics_observe_failed",
            exc=str(metrics_exc),
            exc_type=type(metrics_exc).__name__,
        )
    logger.info(
        "request_complete",
        path=str(request.url.path),
        method=request.method,
        status=response.status_code,
        duration_ms=round(duration_ms, 2),
        request_id=request.headers.get("X-Request-ID"),
    )
    response.headers["X-Response-Time-ms"] = f"{duration_ms:.2f}"
    return response


# Add rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Return consistent error envelope."""
    detail = exc.detail
    if isinstance(detail, dict):
        error_code = detail.get("error", "http_error")
    else:
        error_code = "http_error"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": error_code,
            "detail": detail,
        },
        headers=exc.headers,
    )


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    http_exc = to_http_exception(exc)
    return await http_exception_handler(request, http_exc)


# Include routers
auth_deps = [Depends(verify_api_key)]
app.include_router(routes.health.router)  # Health check is public (for Docker/K8s)
app.include_router(public_demo.router)
app.include_router(studio_router)
app.include_router(studio_internal_router)
app.include_router(routes.feedback.router, prefix="/v1", dependencies=auth_deps)
app.include_router(runs.router, prefix="/v1/runs", dependencies=auth_deps)
app.include_router(chat.router, dependencies=auth_deps)
app.include_router(telegram.router)
app.include_router(webhooks.router)
app.include_router(media.router)
app.include_router(langgraph.router, dependencies=auth_deps)  # LangGraph v2 API
app.include_router(admin.router)  # Admin endpoints (already has auth_deps)
app.include_router(metrics_route.router)


__all__ = ["app", "verify_api_key", "api_key_header", "limiter"]


# AISMR is served from the same origin as its visitor API.
_studio_web_root = _Path(__file__).resolve().parents[3] / "web" / "demo"
if _studio_web_root.is_dir():
    app.mount("/", _StaticFiles(directory=str(_studio_web_root), html=True), name="studio-web")
