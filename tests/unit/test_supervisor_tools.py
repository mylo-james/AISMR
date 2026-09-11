from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from myloware.agents.tools.supervisor import (
    ApproveGateTool,
    GetRunStatusTool,
    ListRunsTool,
    StartWorkflowTool,
)
from myloware.storage.models import RunStatus


class _FakeRun:
    def __init__(
        self,
        run_id: UUID | None = None,
        status: RunStatus | str = RunStatus.RUNNING,
        current_step: str | None = "ideation",
        artifacts: dict[str, object] | None = None,
        workflow_name: str = "aismr",
    ) -> None:
        self.id = run_id or uuid4()
        self.status = status
        self.current_step = current_step
        self.artifacts = artifacts or {}
        self.workflow_name = workflow_name


class _FakeQuery:
    def __init__(self, run: _FakeRun | None) -> None:
        self._run = run

    def filter(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return self

    def order_by(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return self

    def first(self) -> _FakeRun | None:
        return self._run


class _FakeSession:
    def __init__(self, run: _FakeRun | None) -> None:
        self._run = run

    def query(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return _FakeQuery(self._run)


class _FakeRunRepo:
    def __init__(self, run: _FakeRun | None) -> None:
        self.session = _FakeSession(run)
        self._run = run

    def get(self, _run_id: UUID) -> _FakeRun | None:
        return self._run

    def list(self, limit: int = 10):  # type: ignore[no-untyped-def]
        return [self._run] if self._run else []


class _FakeArtifactRepo:
    pass


def test_start_workflow_dedupe_returns_existing() -> None:
    existing = _FakeRun(status=RunStatus.RUNNING, current_step="ideation")
    called = {"count": 0}

    def fake_orchestrator(**_kwargs):  # type: ignore[no-untyped-def]
        called["count"] += 1
        return SimpleNamespace(
            run_id=uuid4(),
            status=RunStatus.RUNNING,
            current_step="ideation",
        )

    tool = StartWorkflowTool(
        client_factory=lambda: object(),
        run_repo_factory=lambda: _FakeRunRepo(existing),
        artifact_repo_factory=lambda: _FakeArtifactRepo(),
        orchestrator=fake_orchestrator,
    )

    result = tool.run_impl(project="aismr", brief="brief", user_id="user-1")
    assert result["deduped"] is True
    assert result["run_id"] == str(existing.id)
    assert called["count"] == 0


def test_start_workflow_executes_orchestrator_when_no_dedupe() -> None:
    called = {"count": 0}

    def fake_orchestrator(**_kwargs):  # type: ignore[no-untyped-def]
        called["count"] += 1
        return SimpleNamespace(
            run_id=uuid4(),
            status=RunStatus.RUNNING,
            current_step="producer",
        )

    tool = StartWorkflowTool(
        client_factory=lambda: object(),
        run_repo_factory=lambda: _FakeRunRepo(None),
        artifact_repo_factory=lambda: _FakeArtifactRepo(),
        orchestrator=fake_orchestrator,
    )

    result = tool.run_impl(project="aismr", brief="brief")
    assert result["deduped"] is False
    assert result["current_step"] == "producer"
    assert called["count"] == 1


@pytest.mark.parametrize("project", [None, 1, "", "../aismr", "unknown-project"])
def test_start_workflow_rejects_invalid_project_before_effects(project: object) -> None:
    calls = {"client": 0, "run_repo": 0, "artifact_repo": 0, "orchestrator": 0}

    def client_factory() -> object:
        calls["client"] += 1
        return object()

    def run_repo_factory() -> _FakeRunRepo:
        calls["run_repo"] += 1
        return _FakeRunRepo(None)

    def artifact_repo_factory() -> _FakeArtifactRepo:
        calls["artifact_repo"] += 1
        return _FakeArtifactRepo()

    def orchestrator(**_kwargs):  # type: ignore[no-untyped-def]
        calls["orchestrator"] += 1
        raise AssertionError("invalid project must not reach the orchestrator")

    tool = StartWorkflowTool(
        client_factory=client_factory,
        run_repo_factory=run_repo_factory,
        artifact_repo_factory=artifact_repo_factory,
        orchestrator=orchestrator,
    )

    result = tool.run_impl(project=project, brief="brief")  # type: ignore[arg-type]

    assert result["error"] is True
    assert result["error_type"] == "invalid_project"
    assert calls == {"client": 0, "run_repo": 0, "artifact_repo": 0, "orchestrator": 0}


@pytest.mark.asyncio
async def test_start_workflow_run_impl_thread_path(monkeypatch) -> None:
    tool = StartWorkflowTool(client_factory=lambda: object())
    monkeypatch.setattr(tool, "_execute_sync", lambda *args, **_kwargs: {"run_id": "abc"})

    # Running inside an event loop triggers the thread-path in run_impl.
    assert asyncio.get_running_loop()
    result = tool.run_impl(project="aismr", brief="brief")
    assert result["run_id"] == "abc"


def test_get_run_status_tool_with_factory() -> None:
    run = _FakeRun(status=RunStatus.COMPLETED, current_step="done")
    tool = GetRunStatusTool(run_repo_factory=lambda: _FakeRunRepo(run))
    result = tool.run_impl(str(run.id))
    assert result["run_id"] == str(run.id)
    assert result["status"] == run.status
    assert result["current_step"] == "done"


def test_get_run_status_tool_raises_for_missing_run() -> None:
    tool = GetRunStatusTool(run_repo_factory=lambda: _FakeRunRepo(None))
    with pytest.raises(ValueError, match="Run not found"):
        tool.run_impl(str(uuid4()))


def test_list_runs_tool_serializes_dict_and_object() -> None:
    run = _FakeRun()
    tool = ListRunsTool(run_repo_factory=lambda: _FakeRunRepo(run))
    result = tool.run_impl(limit=2)
    assert result["runs"][0]["run_id"] == str(run.id)

    class DummyRepo:
        def list(self, limit: int = 10):  # type: ignore[no-untyped-def]
            return [{"id": uuid4(), "workflow_name": "aismr", "status": "done"}]

    tool = ListRunsTool(run_repo_factory=lambda: DummyRepo())
    result = tool.run_impl(limit=1)
    assert result["runs"][0]["workflow_name"] == "aismr"


def test_approve_gate_tool_uses_result_status_value() -> None:
    run_id = uuid4()

    def fake_gate_approver(**_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(run_id=run_id, status=RunStatus.COMPLETED, current_step="done")

    tool = ApproveGateTool(
        client_factory=lambda: object(),
        run_repo_factory=lambda: _FakeRunRepo(_FakeRun(run_id=run_id)),
        artifact_repo_factory=lambda: _FakeArtifactRepo(),
        gate_approver=fake_gate_approver,
    )

    result = tool.run_impl(run_id=str(run_id), gate="publish")
    assert result["status"] == RunStatus.COMPLETED.value
    assert result["current_step"] == "done"


def test_tool_metadata_helpers() -> None:
    assert StartWorkflowTool().get_name() == "start_workflow"
    assert "workflow" in StartWorkflowTool().get_description().lower()
    assert "project" in StartWorkflowTool().get_input_schema()["properties"]

    assert GetRunStatusTool().get_name() == "get_run_status"
    assert "status" in GetRunStatusTool().get_description().lower()
    assert "run_id" in GetRunStatusTool().get_input_schema()["properties"]

    assert ListRunsTool().get_name() == "list_runs"
    assert "recent" in ListRunsTool().get_description().lower()
    assert "limit" in ListRunsTool().get_input_schema()["properties"]

    assert ApproveGateTool().get_name() == "approve_gate"
    assert "approve" in ApproveGateTool().get_description().lower()
    assert "run_id" in ApproveGateTool().get_input_schema()["properties"]


def test_start_workflow_async_run_impl() -> None:
    tool = StartWorkflowTool(client_factory=lambda: object())
    tool._execute_sync = lambda *args, **_kwargs: {"run_id": "abc"}  # type: ignore[assignment]
    result = asyncio.run(tool.async_run_impl(project="aismr", brief="brief"))
    assert result["run_id"] == "abc"


def test_start_workflow_default_session_path(monkeypatch) -> None:
    from myloware.agents.tools import supervisor as sup

    class DummySession:
        pass

    class DummySessionCM:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return DummySession()

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

    def fake_get_session():
        return DummySessionCM()

    run = _FakeRun(status=RunStatus.RUNNING, current_step="ideation")

    class DummyRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            self.session = _FakeSession(None)

    def fake_orchestrator(**_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(run_id=run.id, status=RunStatus.RUNNING, current_step="ideation")

    monkeypatch.setattr(sup, "get_session", fake_get_session)
    monkeypatch.setattr(sup, "RunRepository", lambda _s: DummyRepo(_s))
    monkeypatch.setattr(sup, "ArtifactRepository", lambda _s: _FakeArtifactRepo())

    tool = StartWorkflowTool(
        client_factory=lambda: object(),
        orchestrator=fake_orchestrator,
        enable_dedupe=False,
    )
    out = tool.run_impl(project="aismr", brief="brief")
    assert out["run_id"] == str(run.id)


def test_get_run_status_and_list_runs_default_session(monkeypatch) -> None:
    from myloware.agents.tools import supervisor as sup

    run = _FakeRun()

    class DummyRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            self._run = run

        def get(self, _run_id):  # type: ignore[no-untyped-def]
            return self._run

        def list(self, limit: int = 10):  # type: ignore[no-untyped-def]
            return [self._run]

    class DummySessionCM:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return object()

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(sup, "get_session", lambda: DummySessionCM())
    monkeypatch.setattr(sup, "RunRepository", lambda _s: DummyRepo(_s))

    status_tool = GetRunStatusTool()
    assert status_tool.run_impl(str(run.id))["run_id"] == str(run.id)

    list_tool = ListRunsTool()
    assert list_tool.run_impl()["runs"][0]["run_id"] == str(run.id)


def test_approve_gate_default_session(monkeypatch) -> None:
    from myloware.agents.tools import supervisor as sup

    run_id = uuid4()

    def fake_gate_approver(**_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(run_id=run_id, status="done", current_step="publish")

    class DummyRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            return None

    class DummySessionCM:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return object()

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(sup, "get_session", lambda: DummySessionCM())
    monkeypatch.setattr(sup, "RunRepository", lambda _s: DummyRepo(_s))
    monkeypatch.setattr(sup, "ArtifactRepository", lambda _s: _FakeArtifactRepo())

    tool = ApproveGateTool(client_factory=lambda: object(), gate_approver=fake_gate_approver)
    result = tool.run_impl(run_id=str(run_id), gate="publish")
    assert result["status"] == "done"
