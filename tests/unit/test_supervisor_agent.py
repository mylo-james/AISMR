"""Tests for Supervisor supervisor agent and tools."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from myloware.agents.tools.supervisor import (
    ApproveGateTool,
    GetRunStatusTool,
    ListRunsTool,
    StartWorkflowTool,
)
from myloware.storage.models import RunStatus
from myloware.tools.role_knowledge import RoleKnowledgeSearchTool


def test_create_supervisor_agent_includes_tools():
    from myloware.agents.supervisor import create_supervisor_agent

    mock_client = MagicMock()
    mock_agent = MagicMock()

    with patch(
        "myloware.agents.supervisor.create_persona_agent", return_value=mock_agent
    ) as mock_create:
        agent = create_supervisor_agent(mock_client)
        assert agent == mock_agent
        tools = mock_create.call_args.kwargs["tools"]
        assert any(isinstance(t, StartWorkflowTool) for t in tools)
        assert any(isinstance(t, GetRunStatusTool) for t in tools)
        assert any(isinstance(t, ListRunsTool) for t in tools)
        assert any(isinstance(t, ApproveGateTool) for t in tools)
        assert any(isinstance(t, RoleKnowledgeSearchTool) for t in tools)
        assert not any(isinstance(t, dict) and t.get("type") == "file_search" for t in tools)


def test_create_supervisor_agent_requires_instructions(monkeypatch) -> None:
    from myloware.agents import supervisor as supervisor_mod

    monkeypatch.setattr(supervisor_mod, "load_agent_config", lambda *_a, **_kw: {})

    with pytest.raises(ValueError, match="No instructions found"):
        supervisor_mod.create_supervisor_agent(MagicMock())


def test_start_workflow_tool_run_impl_uses_orchestrator():
    fake_run_id = uuid.uuid4()

    def fake_orchestrator(**_kwargs):
        return SimpleNamespace(run_id=fake_run_id, status=RunStatus.RUNNING, current_step="ideator")

    tool = StartWorkflowTool(
        client_factory=lambda: object(),
        run_repo_factory=lambda: None,
        artifact_repo_factory=lambda: None,
        vector_db_id="kb",
        orchestrator=fake_orchestrator,
    )

    # run_impl is synchronous, not async
    result = tool.run_impl(project="aismr", brief="demo")

    assert result["run_id"] == str(fake_run_id)
    assert result["status"] == RunStatus.RUNNING.value


def test_get_run_status_tool_returns_data():
    run_id = uuid.uuid4()
    run_obj = SimpleNamespace(
        id=run_id,
        workflow_name="aismr",
        status=RunStatus.RUNNING.value,
        current_step="ideator",
        artifacts={"ideas": "cool"},
    )
    tool = GetRunStatusTool(run_repo_factory=lambda: SimpleNamespace(get=lambda _id: run_obj))

    # run_impl is synchronous, not async
    result = tool.run_impl(run_id=str(run_id))

    assert result["run_id"] == str(run_id)
    assert result["status"] == RunStatus.RUNNING.value
    assert result["artifacts"]["ideas"] == "cool"


def test_list_runs_tool_serializes_runs():
    runs = [
        SimpleNamespace(
            id=uuid.uuid4(), workflow_name="aismr", status="running", current_step="ideator"
        ),
        {"id": uuid.uuid4(), "workflow_name": "test", "status": "completed"},
    ]
    tool = ListRunsTool(run_repo_factory=lambda: SimpleNamespace(list=lambda limit=10: runs))

    # run_impl is synchronous, not async
    result = tool.run_impl(limit=5)
    assert len(result["runs"]) == 2
    assert result["runs"][0]["workflow_name"] == "aismr"


def test_approve_gate_tool_calls_gate_approver():
    run_id = uuid.uuid4()

    def fake_approver(**_kwargs):
        return SimpleNamespace(run_id=run_id, status=RunStatus.COMPLETED, current_step="publisher")

    tool = ApproveGateTool(
        client_factory=lambda: object(),
        run_repo_factory=lambda: SimpleNamespace(
            get=lambda _run_id: SimpleNamespace(workflow_name="motivational")
        ),
        artifact_repo_factory=lambda: object(),
        vector_db_id="kb",
        gate_approver=fake_approver,
    )

    # run_impl is synchronous, not async
    result = tool.run_impl(run_id=str(run_id), gate="publish")

    assert result["run_id"] == str(run_id)
    assert result["status"] == RunStatus.COMPLETED.value
