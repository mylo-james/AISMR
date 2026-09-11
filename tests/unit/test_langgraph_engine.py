from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.types import Command

from myloware.config import settings
from myloware.storage.database import get_async_session_factory, init_async_db
from myloware.storage.models import RunStatus
from myloware.storage.repositories import RunRepository
from myloware.workflows.langgraph.graph import LangGraphEngine, _GraphWrapper


@pytest.mark.asyncio
async def test_engine_sqlite_compiles_without_checkpointer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///:memory:")
    engine = LangGraphEngine()
    # No Postgres saver required in SQLite mode.
    await engine.ensure_checkpointer_initialized()
    assert engine.get_graph() is not None


def test_engine_postgres_requires_explicit_init(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "database_url", "postgresql+psycopg2://user:pass@localhost:5432/myloware"
    )
    engine = LangGraphEngine()
    with pytest.raises(RuntimeError, match="Durable LangGraph checkpointer not initialized"):
        engine.get_graph()


@pytest.mark.asyncio
async def test_engine_reopens_checkpointer_on_loop_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "database_url", "postgresql+psycopg2://user:pass@localhost:5432/myloware"
    )

    contexts: list[SimpleNamespace] = []

    class FakeConn:
        closed = False

    class FakeSaver:
        def __init__(self) -> None:
            self.loop = asyncio.get_running_loop()
            self.conn = FakeConn()

        async def setup(self) -> None:
            return None

    class FakeCtx:
        def __init__(self) -> None:
            self.exited = 0
            self.saver = FakeSaver()

        async def __aenter__(self) -> FakeSaver:
            return self.saver

        async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            self.exited += 1

    def fake_from_conn_string(_dsn: str) -> FakeCtx:
        ctx = FakeCtx()
        contexts.append(SimpleNamespace(ctx=ctx))
        return ctx

    monkeypatch.setattr(
        "myloware.workflows.langgraph.graph.AsyncPostgresSaver.from_conn_string",
        fake_from_conn_string,
    )

    engine = LangGraphEngine()
    await engine.ensure_checkpointer_initialized()
    assert len(contexts) == 1

    # Second call should reuse existing saver.
    await engine.ensure_checkpointer_initialized()
    assert len(contexts) == 1

    # Simulate loop mismatch: force the saver.loop to be "different".
    assert engine._async_checkpointer is not None
    engine._async_checkpointer.loop = object()

    await engine.ensure_checkpointer_initialized()
    assert len(contexts) == 2
    assert contexts[0].ctx.exited == 1


@pytest.mark.asyncio
async def test_engine_shutdown_closes_checkpointer(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "database_url", "postgresql+psycopg2://user:pass@localhost:5432/myloware"
    )

    ctx = SimpleNamespace(exited=0)

    class FakeConn:
        closed = False

    class FakeSaver:
        def __init__(self) -> None:
            self.loop = asyncio.get_running_loop()
            self.conn = FakeConn()

        async def setup(self) -> None:
            return None

    class FakeCtx:
        async def __aenter__(self) -> FakeSaver:
            return FakeSaver()

        async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            ctx.exited += 1

    monkeypatch.setattr(
        "myloware.workflows.langgraph.graph.AsyncPostgresSaver.from_conn_string",
        lambda _dsn: FakeCtx(),
    )

    engine = LangGraphEngine()
    await engine.ensure_checkpointer_initialized()
    await engine.shutdown()
    assert ctx.exited == 1


@pytest.mark.asyncio
async def test_graph_wrapper_persists_db_projection_after_resume(monkeypatch) -> None:
    """Command(resume=...) should snapshot status/current_step into runs table."""
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///:memory:")
    await init_async_db()

    run_id = uuid4()
    SessionLocal = get_async_session_factory()
    async with SessionLocal() as session:
        from myloware.storage.models import Run

        run = Run(
            id=run_id,
            workflow_name="aismr",
            input="brief",
            status=RunStatus.PENDING.value,
        )
        session.add(run)
        await session.commit()

    class FakeGraph:
        async def ainvoke(self, _input, config=None, **kwargs):  # type: ignore[no-untyped-def]
            return {"status": RunStatus.COMPLETED.value, "current_step": "completed", "error": None}

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                values={"status": RunStatus.RUNNING.value, "current_step": "publishing"}
            )

    wrapper = _GraphWrapper(FakeGraph())
    cfg = {"configurable": {"thread_id": str(run_id), "checkpoint_id": "cp-1"}}
    await wrapper.ainvoke(Command(resume={"dummy": {"approved": True}}), config=cfg)

    async with SessionLocal() as session:
        repo = RunRepository(session)
        updated = await repo.get_async(run_id)
        assert updated is not None
        assert updated.status == RunStatus.COMPLETED.value
        assert updated.current_step == "completed"
