"""Durability checks for the file-backed SQLite LangGraph saver."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from myloware.config import settings
from myloware.workflows.langgraph.graph import LangGraphEngine


@pytest.mark.asyncio
async def test_file_sqlite_requires_initialization_and_creates_sibling_saver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    engine = LangGraphEngine()

    with pytest.raises(RuntimeError, match="not initialized"):
        engine.get_graph()

    await engine.ensure_checkpointer_initialized()
    assert (tmp_path / "app.langgraph-checkpoints.sqlite3").exists()
    assert await engine.check_checkpointer_health() is True
    await engine.shutdown()


def test_file_sqlite_interrupt_resumes_in_a_separate_process(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    thread_id = str(uuid4())
    program = r"""
import asyncio
import sys
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Command, interrupt
from myloware.config import settings
from myloware.workflows.langgraph import graph as graph_mod

async def pause(state):
    value = interrupt({"gate": "approval"})
    return {"answer": value}

async def main():
    settings.database_url = sys.argv[1]
    engine = graph_mod.LangGraphEngine()
    await engine.ensure_checkpointer_initialized()
    builder = StateGraph(dict)
    builder.add_node("pause", pause)
    builder.add_edge(START, "pause")
    builder.add_edge("pause", END)
    graph_mod.get_compiled_graph = lambda checkpointer=None: builder.compile(checkpointer=checkpointer)
    graph = engine.get_graph()
    config = {"configurable": {"thread_id": sys.argv[2]}}
    if sys.argv[3] == "start":
        result = await graph.ainvoke({"answer": None}, config=config)
        assert "__interrupt__" in result
    else:
        result = await graph.ainvoke(Command(resume="approved"), config=config)
        assert result["answer"] == "approved"
    await engine.shutdown()

asyncio.run(main())
"""
    env = {**os.environ, "PYTHONPATH": str(Path.cwd() / "src")}
    for phase in ("start", "resume"):
        completed = subprocess.run(
            [sys.executable, "-c", program, database_url, thread_id, phase],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_memory_sqlite_is_not_reported_as_durable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///:memory:")
    engine = LangGraphEngine()

    await engine.ensure_checkpointer_initialized()
    assert await engine.check_checkpointer_health() is False
