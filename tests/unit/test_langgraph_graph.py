from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langgraph.types import Command

from myloware.workflows.langgraph import graph as graph_mod


def test_route_helpers() -> None:
    assert graph_mod.route_after_ideation_approval({"ideas_approved": True}) == "production"
    assert graph_mod.route_after_ideation_approval({"ideas_approved": False}) == graph_mod.END

    assert (
        graph_mod.route_after_production({"video_clips": ["x"], "production_complete": True})
        == "editing"
    )
    assert graph_mod.route_after_production({}) == "wait_for_videos"

    assert graph_mod.route_after_editing({"final_video_url": "x"}) == "publish_approval"
    assert graph_mod.route_after_editing({}) == "wait_for_render"

    assert graph_mod.route_after_publish_approval({"publish_approved": True}) == "publishing"
    assert graph_mod.route_after_publish_approval({}) == graph_mod.END

    assert graph_mod.route_after_publishing({}) == graph_mod.END


def test_graph_wrapper_config_helpers() -> None:
    wrapper = graph_mod._GraphWrapper(graph=None)

    config = {"configurable": {"thread_id": "abc", "checkpoint_id": "cp1"}}
    assert wrapper._thread_id_from_config(config) == "abc"
    stripped = wrapper._without_checkpoint_id(config)
    assert stripped["configurable"].get("checkpoint_id") is None

    assert wrapper._thread_id_from_config(None) is None


@pytest.mark.asyncio
async def test_graph_wrapper_persists_run_snapshot(monkeypatch) -> None:
    run_id = uuid4()
    updates: dict[str, object] = {}

    class FakeGraph:
        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                values={"status": "running", "current_step": "ideation", "error": None}
            )

    class FakeSession:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

        async def commit(self):  # type: ignore[no-untyped-def]
            updates["committed"] = True

    class FakeRunRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            return None

        async def update_async(self, _run_id, **kwargs):  # type: ignore[no-untyped-def]
            updates.update(kwargs)

    monkeypatch.setattr(graph_mod, "RunRepository", lambda _s: FakeRunRepo(_s))
    monkeypatch.setattr(graph_mod, "get_async_session_factory", lambda: (lambda: FakeSession()))

    wrapper = graph_mod._GraphWrapper(FakeGraph())
    config = {"configurable": {"thread_id": str(run_id), "checkpoint_id": "cp1"}}

    await wrapper._persist_run_snapshot(config, result={"error": "boom"})

    assert updates["status"] == "running"
    assert updates["current_step"] == "ideation"
    assert updates["error"] == "boom"
    assert updates["committed"] is True


@pytest.mark.asyncio
async def test_graph_wrapper_ainvoke_persists_on_resume(monkeypatch) -> None:
    class FakeGraph:
        async def ainvoke(self, *_a, **_k):  # type: ignore[no-untyped-def]
            return {"status": "running"}

    wrapper = graph_mod._GraphWrapper(FakeGraph())
    persist = AsyncMock()
    monkeypatch.setattr(wrapper, "_persist_run_snapshot", persist)

    await wrapper.ainvoke(Command(resume={"x": 1}), config={"configurable": {"thread_id": "t"}})
    persist.assert_awaited_once()

    persist.reset_mock()
    await wrapper.ainvoke({"x": 1}, config={"configurable": {"thread_id": "t"}})
    persist.assert_not_awaited()


def test_langgraph_engine_get_graph_caches(monkeypatch) -> None:
    engine = graph_mod.LangGraphEngine()

    monkeypatch.setattr(graph_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")

    def fake_compiled_graph(checkpointer=None):  # type: ignore[no-untyped-def]
        return "graph-sqlite"

    monkeypatch.setattr(graph_mod, "get_compiled_graph", fake_compiled_graph)

    graph1 = engine.get_graph()
    graph2 = engine.get_graph()

    assert graph1 is graph2
    assert getattr(graph1, "_graph") == "graph-sqlite"

    monkeypatch.setattr(graph_mod.settings, "database_url", "postgresql://localhost/db")
    engine._async_checkpointer = object()

    def fake_compiled_graph2(checkpointer=None):  # type: ignore[no-untyped-def]
        return "graph-postgres"

    monkeypatch.setattr(graph_mod, "get_compiled_graph", fake_compiled_graph2)

    graph3 = engine.get_graph()
    assert graph3 is not graph1
    assert getattr(graph3, "_graph") == "graph-postgres"


@pytest.mark.asyncio
async def test_check_checkpointer_health_sqlite(monkeypatch) -> None:
    engine = graph_mod.LangGraphEngine()
    monkeypatch.setattr(graph_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")

    assert await engine.check_checkpointer_health() is False


@pytest.mark.asyncio
async def test_enter_async_checkpointer_rejects_sqlite(monkeypatch) -> None:
    engine = graph_mod.LangGraphEngine()
    monkeypatch.setattr(graph_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    with pytest.raises(RuntimeError):
        await engine._enter_async_checkpointer()


@pytest.mark.asyncio
async def test_enter_async_checkpointer_reuses_existing(monkeypatch) -> None:
    engine = graph_mod.LangGraphEngine()
    monkeypatch.setattr(graph_mod.settings, "database_url", "postgresql://localhost/db")

    class FakeConn:
        closed = False

    class FakeSaver:
        def __init__(self, loop):
            self.loop = loop
            self.conn = FakeConn()

    current = asyncio.get_running_loop()
    engine._async_checkpointer = FakeSaver(current)
    engine._async_checkpointer_dsn = "postgresql://localhost/db"

    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("should not create new checkpointer")

    monkeypatch.setattr(graph_mod.AsyncPostgresSaver, "from_conn_string", boom)

    saver = await engine._enter_async_checkpointer()
    assert saver is engine._async_checkpointer


@pytest.mark.asyncio
async def test_enter_async_checkpointer_rebuilds_and_sets_dsn(monkeypatch) -> None:
    engine = graph_mod.LangGraphEngine()
    monkeypatch.setattr(graph_mod.settings, "database_url", "postgresql+psycopg2://localhost/db")

    class FakeCtx:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                loop=asyncio.get_running_loop(),
                conn=SimpleNamespace(closed=False),
                setup=AsyncMock(),
            )

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(graph_mod.AsyncPostgresSaver, "from_conn_string", lambda _dsn: FakeCtx())

    await engine._enter_async_checkpointer()
    assert engine._async_checkpointer_target == "postgresql://localhost/db"


@pytest.mark.asyncio
async def test_checkpointer_health_postgres_success(monkeypatch) -> None:
    engine = graph_mod.LangGraphEngine()
    monkeypatch.setattr(graph_mod.settings, "database_url", "postgresql://localhost/db")

    async def fake_init():
        return None

    fake_setup = AsyncMock()
    monkeypatch.setattr(engine, "ensure_checkpointer_initialized", fake_init)
    monkeypatch.setattr(
        engine, "_get_async_checkpointer", lambda: SimpleNamespace(setup=fake_setup)
    )

    assert await engine.check_checkpointer_health() is True
