from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import Response
from slowapi.errors import RateLimitExceeded


def test_should_init_sentry_respects_env(monkeypatch) -> None:
    from myloware.api import server

    monkeypatch.setattr(server.settings, "sentry_dsn", "dsn")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("DISABLE_SENTRY", raising=False)
    assert server._should_init_sentry() is True

    monkeypatch.setenv("DISABLE_SENTRY", "1")
    assert server._should_init_sentry() is False


def test_should_init_sentry_false_without_dsn(monkeypatch) -> None:
    from myloware.api import server

    monkeypatch.setattr(server.settings, "sentry_dsn", "")
    assert server._should_init_sentry() is False


def test_metrics_path_uses_route_template() -> None:
    from myloware.api import server

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/runs/abc",
        "headers": [],
        "route": SimpleNamespace(path="/v1/runs/{run_id}"),
    }
    request = Request(scope)
    assert server._metrics_path(request) == "/v1/runs/{run_id}"

    scope = {"type": "http", "method": "GET", "path": "/nope", "headers": []}
    request = Request(scope)
    assert server._metrics_path(request) == "unmatched"


@pytest.mark.asyncio
async def test_rate_limit_handler_includes_retry_after(monkeypatch) -> None:
    from myloware.api import server

    scope = {"type": "http", "method": "GET", "path": "/rate", "headers": []}
    request = Request(scope)
    exc = RateLimitExceeded(SimpleNamespace(error_message=None, limit="1/minute"))
    exc.headers = {"Retry-After": "30"}  # type: ignore[assignment]

    resp = await server.rate_limit_handler(request, exc)
    body = json.loads(resp.body)
    assert resp.status_code == 429
    assert body["retry_after_seconds"] == 30
    assert body["limit"] == "1/minute"


@pytest.mark.asyncio
async def test_request_id_and_timing_middlewares_set_headers() -> None:
    from myloware.api import server

    scope = {"type": "http", "method": "GET", "path": "/x", "headers": []}
    request = Request(scope)

    async def call_next(_req: Request):  # type: ignore[no-untyped-def]
        return Response(content="ok")

    resp = await server.request_id_middleware(request, call_next)
    assert resp.headers.get("X-Request-ID")

    resp = await server.timing_middleware(request, call_next)
    assert "X-Response-Time-ms" in resp.headers


@pytest.mark.asyncio
async def test_http_and_domain_error_handlers() -> None:
    from myloware.api import errors, server

    scope = {"type": "http", "method": "GET", "path": "/err", "headers": []}
    request = Request(scope)

    exc = HTTPException(status_code=400, detail={"error": "bad", "detail": "oops"})
    resp = await server.http_exception_handler(request, exc)
    assert resp.status_code == 400
    data = json.loads(resp.body)
    assert data["error"] == "bad"

    derr = errors.DomainError("boom", error="domain", status_code=422)
    resp2 = await server.domain_error_handler(request, derr)
    assert resp2.status_code == 422
    data2 = json.loads(resp2.body)
    assert data2["error"] == "domain"


@pytest.mark.asyncio
async def test_lifespan_real_provider_reuses_store(monkeypatch) -> None:
    from myloware.api import server

    fake_toolgroups = SimpleNamespace(register=lambda **_kwargs: None)

    class FakeShields:
        def list(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                data=[SimpleNamespace(identifier=server.settings.content_safety_shield_id)]
            )

    fake_client = SimpleNamespace(toolgroups=fake_toolgroups, shields=FakeShields())

    monkeypatch.setattr(server.settings, "llama_stack_provider", "real")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "kb_skip_ingest_on_start", False)
    monkeypatch.setattr(server.settings, "project_id", "proj")
    monkeypatch.setattr(server.settings, "database_url", "sqlite:///:memory:")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", False)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server, "get_sync_client", lambda: fake_client)
    monkeypatch.setattr(server, "init_db", lambda: None)
    monkeypatch.setattr(server, "load_manifest", lambda: {"hash": "h1"})
    monkeypatch.setattr(
        server,
        "load_documents_with_manifest",
        lambda *_args, **_kwargs: ([], {"hash": "h1", "files": []}),
    )
    monkeypatch.setattr(server, "save_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "save_manifest", lambda _m: None)
    monkeypatch.setattr(server, "get_existing_vector_store", lambda *_a, **_k: "vs-1")
    monkeypatch.setattr(server, "setup_project_knowledge", lambda *_a, **_k: "vs-2")
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)

    async with server.lifespan(server.app):
        assert server.app.state.vector_db_id == "vs-1"
        assert server.app.state.knowledge_base_healthy is True


@pytest.mark.asyncio
async def test_lifespan_real_provider_skip_ingest(monkeypatch) -> None:
    from myloware.api import server

    fake_toolgroups = SimpleNamespace(register=lambda **_kwargs: None)

    class FakeShields:
        def list(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                data=[SimpleNamespace(identifier=server.settings.content_safety_shield_id)]
            )

    fake_client = SimpleNamespace(toolgroups=fake_toolgroups, shields=FakeShields())

    monkeypatch.setattr(server.settings, "llama_stack_provider", "real")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "kb_skip_ingest_on_start", True)
    monkeypatch.setattr(server.settings, "project_id", "proj")
    monkeypatch.setattr(server.settings, "database_url", "sqlite:///:memory:")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", False)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server, "get_sync_client", lambda: fake_client)
    monkeypatch.setattr(server, "init_db", lambda: None)
    monkeypatch.setattr(server, "load_manifest", lambda: {"hash": "h2"})
    monkeypatch.setattr(
        server,
        "load_documents_with_manifest",
        lambda *_args, **_kwargs: ([], {"hash": "h3", "files": []}),
    )
    monkeypatch.setattr(server, "save_manifest", lambda _m: None)
    monkeypatch.setattr(server, "get_existing_vector_store", lambda *_a, **_k: "vs-3")
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)

    async with server.lifespan(server.app):
        assert server.app.state.vector_db_id == "vs-3"
        assert server.app.state.knowledge_base_healthy is False


@pytest.mark.asyncio
async def test_lifespan_skips_kb_when_fake_provider(monkeypatch) -> None:
    from myloware.api import server

    monkeypatch.setattr(server.settings, "llama_stack_provider", "fake")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "database_url", "sqlite:///:memory:")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", False)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server, "init_db", lambda: None)
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)

    async with server.lifespan(server.app):
        assert server.app.state.knowledge_base_healthy is True
        assert server.app.state.vector_db_id.startswith("project_kb_")


@pytest.mark.asyncio
async def test_lifespan_db_init_failure_sets_flag(monkeypatch) -> None:
    from myloware.api import server

    monkeypatch.setattr(server.settings, "llama_stack_provider", "fake")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "database_url", "sqlite:///:memory:")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", False)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server, "init_db", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)

    async with server.lifespan(server.app):
        assert server.app.state.database_ready is False


@pytest.mark.asyncio
async def test_lifespan_sentry_init_failure(monkeypatch) -> None:
    from myloware.api import server

    monkeypatch.setattr(server.settings, "llama_stack_provider", "fake")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "database_url", "sqlite:///:memory:")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", False)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server, "init_db", lambda: None)
    monkeypatch.setattr(server, "_should_init_sentry", lambda: True)
    monkeypatch.setattr(
        server.sentry_sdk, "init", lambda **_k: (_ for _ in ()).throw(ValueError("dsn"))
    )
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)

    async with server.lifespan(server.app):
        assert server.app.state.database_ready is True


@pytest.mark.asyncio
async def test_lifespan_db_init_failure_raises_when_fail_fast(monkeypatch) -> None:
    from myloware.api import server

    monkeypatch.setattr(server.settings, "llama_stack_provider", "fake")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "database_url", "sqlite:///:memory:")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", False)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", True)
    monkeypatch.setattr(server, "init_db", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)

    with pytest.raises(RuntimeError):
        async with server.lifespan(server.app):
            pass


@pytest.mark.asyncio
async def test_lifespan_langgraph_checkpointer_failure_nonfatal(monkeypatch) -> None:
    from myloware.api import server

    class FakeEngine:
        async def ensure_checkpointer_initialized(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

        async def shutdown(self):  # type: ignore[no-untyped-def]
            return None

    class FakeLangGraphEngine:
        def __init__(self):
            self._engine = FakeEngine()

        async def ensure_checkpointer_initialized(self):  # type: ignore[no-untyped-def]
            return await self._engine.ensure_checkpointer_initialized()

        async def shutdown(self):  # type: ignore[no-untyped-def]
            return await self._engine.shutdown()

    async def fake_init_async_db():  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(server.settings, "llama_stack_provider", "fake")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "database_url", "postgresql+asyncpg://user@host/db")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", True)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server, "init_async_db", fake_init_async_db)
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)
    monkeypatch.setattr("myloware.workflows.langgraph.graph.LangGraphEngine", FakeLangGraphEngine)
    async with server.lifespan(server.app):
        assert server.app.state.database_ready is True


@pytest.mark.asyncio
async def test_lifespan_rag_toolgroup_registration_failure_raises(monkeypatch) -> None:
    from myloware.api import server

    class FakeToolgroups:
        def register(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("rag down")

    class FakeShields:
        def list(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(data=[])

    fake_client = SimpleNamespace(toolgroups=FakeToolgroups(), shields=FakeShields())

    monkeypatch.setattr(server.settings, "llama_stack_provider", "real")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "kb_skip_ingest_on_start", False)
    monkeypatch.setattr(server.settings, "project_id", "proj")
    monkeypatch.setattr(server.settings, "database_url", "sqlite:///:memory:")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", False)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server, "get_sync_client", lambda: fake_client)
    monkeypatch.setattr(server, "init_db", lambda: None)
    monkeypatch.setattr(
        server,
        "load_documents_with_manifest",
        lambda *_args, **_kwargs: ([], {"hash": "h1", "files": []}),
    )
    monkeypatch.setattr(server, "save_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "get_existing_vector_store", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "setup_project_knowledge", lambda *_a, **_k: "vs-1")
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)

    with pytest.raises(RuntimeError, match="Failed to register RAG toolgroup"):
        async with server.lifespan(server.app):
            pass


@pytest.mark.asyncio
async def test_lifespan_shields_missing_sets_flag(monkeypatch) -> None:
    from myloware.api import server

    class FakeToolgroups:
        def register(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

    class FakeShields:
        def list(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(data=[])

    fake_client = SimpleNamespace(toolgroups=FakeToolgroups(), shields=FakeShields())

    monkeypatch.setattr(server.settings, "llama_stack_provider", "real")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "kb_skip_ingest_on_start", False)
    monkeypatch.setattr(server.settings, "project_id", "proj")
    monkeypatch.setattr(server.settings, "database_url", "sqlite:///:memory:")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", False)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server.settings, "brave_api_key", "")
    monkeypatch.setattr(server, "get_sync_client", lambda: fake_client)
    monkeypatch.setattr(server, "init_db", lambda: None)
    monkeypatch.setattr(
        server,
        "load_documents_with_manifest",
        lambda *_args, **_kwargs: ([], {"hash": "h1", "files": []}),
    )
    monkeypatch.setattr(server, "save_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "get_existing_vector_store", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "setup_project_knowledge", lambda *_a, **_k: "vs-1")
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)

    async with server.lifespan(server.app):
        assert server.app.state.shields_available is False


@pytest.mark.asyncio
async def test_lifespan_shutdown_handles_engine_failure(monkeypatch) -> None:
    from myloware.api import server

    class FakeEngine:
        async def ensure_checkpointer_initialized(self):  # type: ignore[no-untyped-def]
            return None

        async def shutdown(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("shutdown")

    monkeypatch.setattr(server.settings, "llama_stack_provider", "fake")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "database_url", "sqlite:///:memory:")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", True)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server, "init_db", lambda: None)
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)

    monkeypatch.setattr("myloware.workflows.langgraph.graph.LangGraphEngine", lambda: FakeEngine())
    async with server.lifespan(server.app):
        assert server.app.state.database_ready is True


@pytest.mark.asyncio
async def test_lifespan_missing_langgraph_engine_nonfatal(monkeypatch) -> None:
    from myloware.api import server

    async def fake_init_async_db():  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(server.settings, "llama_stack_provider", "fake")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "database_url", "postgresql+asyncpg://user@host/db")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", True)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server, "init_async_db", fake_init_async_db)
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)
    monkeypatch.setattr("myloware.workflows.langgraph.graph.LangGraphEngine", lambda: None)

    async with server.lifespan(server.app):
        assert server.app.state.database_ready is True


@pytest.mark.asyncio
async def test_lifespan_checkpointer_init_success(monkeypatch) -> None:
    from myloware.api import server

    class FakeEngine:
        async def ensure_checkpointer_initialized(self):  # type: ignore[no-untyped-def]
            return None

        async def shutdown(self):  # type: ignore[no-untyped-def]
            return None

    class FakeLangGraphEngine:
        async def ensure_checkpointer_initialized(self):  # type: ignore[no-untyped-def]
            return None

        async def shutdown(self):  # type: ignore[no-untyped-def]
            return None

    async def fake_init_async_db():  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(server.settings, "llama_stack_provider", "fake")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "database_url", "postgresql+asyncpg://user@host/db")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", True)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server, "init_async_db", fake_init_async_db)
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)
    monkeypatch.setattr("myloware.workflows.langgraph.graph.LangGraphEngine", FakeLangGraphEngine)

    server.app.state.langgraph_engine = FakeEngine()
    async with server.lifespan(server.app):
        assert server.app.state.database_ready is True


@pytest.mark.asyncio
async def test_lifespan_checkpointer_init_failure_fail_fast(monkeypatch) -> None:
    from myloware.api import server

    class FakeLangGraphEngine:
        async def ensure_checkpointer_initialized(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

        async def shutdown(self):  # type: ignore[no-untyped-def]
            return None

    async def fake_init_async_db():  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(server.settings, "llama_stack_provider", "fake")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "database_url", "postgresql+asyncpg://user@host/db")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", True)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", True)
    monkeypatch.setattr(server, "init_async_db", fake_init_async_db)
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)
    monkeypatch.setattr("myloware.workflows.langgraph.graph.LangGraphEngine", FakeLangGraphEngine)

    with pytest.raises(RuntimeError):
        async with server.lifespan(server.app):
            pass


@pytest.mark.asyncio
async def test_lifespan_websearch_registration_failure(monkeypatch) -> None:
    from myloware.api import server

    class FakeToolgroups:
        def register(self, **kwargs):  # type: ignore[no-untyped-def]
            if kwargs.get("provider_id") == "brave-search":
                raise RuntimeError("brave down")
            return None

    class FakeShields:
        def list(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                data=[SimpleNamespace(identifier=server.settings.content_safety_shield_id)]
            )

    fake_client = SimpleNamespace(toolgroups=FakeToolgroups(), shields=FakeShields())

    monkeypatch.setattr(server.settings, "llama_stack_provider", "real")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "kb_skip_ingest_on_start", False)
    monkeypatch.setattr(server.settings, "project_id", "proj")
    monkeypatch.setattr(server.settings, "database_url", "sqlite:///:memory:")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", False)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server.settings, "brave_api_key", "key")
    monkeypatch.setattr(server, "get_sync_client", lambda: fake_client)
    monkeypatch.setattr(server, "init_db", lambda: None)
    monkeypatch.setattr(
        server,
        "load_documents_with_manifest",
        lambda *_args, **_kwargs: ([], {"hash": "h1", "files": []}),
    )
    monkeypatch.setattr(server, "save_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "get_existing_vector_store", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "setup_project_knowledge", lambda *_a, **_k: "vs-1")
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)

    async with server.lifespan(server.app):
        assert server.app.state.shields_available is True


@pytest.mark.asyncio
async def test_lifespan_shields_check_failure(monkeypatch) -> None:
    from myloware.api import server

    class FakeToolgroups:
        def register(self, **kwargs):  # type: ignore[no-untyped-def]
            return None

    class FakeShields:
        def list(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("shields down")

    fake_client = SimpleNamespace(toolgroups=FakeToolgroups(), shields=FakeShields())

    monkeypatch.setattr(server.settings, "llama_stack_provider", "real")
    monkeypatch.setattr(server.settings, "use_fake_providers", False)
    monkeypatch.setattr(server.settings, "kb_skip_ingest_on_start", False)
    monkeypatch.setattr(server.settings, "project_id", "proj")
    monkeypatch.setattr(server.settings, "database_url", "sqlite:///:memory:")
    monkeypatch.setattr(server.settings, "use_langgraph_engine", False)
    monkeypatch.setattr(server.settings, "fail_fast_on_startup", False)
    monkeypatch.setattr(server, "get_sync_client", lambda: fake_client)
    monkeypatch.setattr(server, "init_db", lambda: None)
    monkeypatch.setattr(
        server,
        "load_documents_with_manifest",
        lambda *_args, **_kwargs: ([], {"hash": "h1", "files": []}),
    )
    monkeypatch.setattr(server, "save_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "get_existing_vector_store", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "setup_project_knowledge", lambda *_a, **_k: "vs-1")
    monkeypatch.setattr("myloware.observability.init_observability", lambda: None)

    async with server.lifespan(server.app):
        assert server.app.state.shields_available is False
