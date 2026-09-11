"""Unit tests for CLI commands."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from importlib.metadata import version
from types import SimpleNamespace
from uuid import uuid4

from click.testing import CliRunner

from myloware.cli.main import cli
from myloware.storage.models import RunStatus
from myloware.workflows.state import WorkflowResult


def test_version_command():
    """aismr --version shows package version."""

    result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert version("aismr") in result.output


def test_worker_group_help():
    """worker group is available."""

    result = CliRunner().invoke(cli, ["worker", "--help"])

    assert result.exit_code == 0
    assert "Worker" in result.output


def test_runs_list(monkeypatch):
    """runs list prints recent runs in table form."""

    runs = [
        SimpleNamespace(
            id="run-1",
            workflow_name="aismr",
            status=RunStatus.RUNNING.value,
            current_step="ideator",
            created_at=datetime(2025, 12, 5, 12, 0, 0),
        )
    ]

    class FakeRunRepo:
        def __init__(self, _session):
            pass

        def list(self, limit: int = 10, offset: int = 0, status=None):
            assert limit == 5
            return runs

    @contextmanager
    def fake_session():
        yield SimpleNamespace(expire_all=lambda: None)

    monkeypatch.setattr("myloware.cli.runs.RunRepository", FakeRunRepo)
    monkeypatch.setattr("myloware.cli.runs.get_session", fake_session)

    result = CliRunner().invoke(cli, ["runs", "list", "--limit", "5"])

    assert result.exit_code == 0
    assert "aismr" in result.output
    assert RunStatus.RUNNING.value in result.output


def test_runs_watch(monkeypatch):
    """runs watch polls until terminal status."""

    statuses = [
        RunStatus.RUNNING.value,
        RunStatus.AWAITING_IDEATION_APPROVAL.value,
        RunStatus.COMPLETED.value,
    ]

    class FakeRunRepo:
        def __init__(self, _session):
            self.calls = 0

        def get_by_id_str(self, run_id: str):
            status = statuses[min(self.calls, len(statuses) - 1)]
            self.calls += 1
            return SimpleNamespace(id=run_id, status=status, current_step=None)

    @contextmanager
    def fake_session():
        yield SimpleNamespace(expire_all=lambda: None)

    monkeypatch.setattr("myloware.cli.runs.RunRepository", FakeRunRepo)
    monkeypatch.setattr("myloware.cli.runs.get_session", fake_session)
    monkeypatch.setattr("myloware.cli.runs.time.sleep", lambda _s: None)

    result = CliRunner().invoke(cli, ["runs", "watch", "abc", "--interval", "0"])

    assert result.exit_code == 0
    assert RunStatus.COMPLETED.value in result.output
    assert "finished" in result.output


def test_demo_aismr_interactive(monkeypatch):
    """demo aismr runs workflow and approvals."""

    run_id = uuid4()
    calls = {"start": 0, "ideation": 0, "publish": 0}

    def fake_run_workflow(client, brief, vector_db_id, run_repo, artifact_repo, workflow_name):
        calls["start"] += 1
        assert brief == "demo brief"
        assert vector_db_id == "project_kb_aismr"
        return WorkflowResult(
            run_id=str(run_id),
            status=RunStatus.AWAITING_IDEATION_APPROVAL.value,
            current_step="ideator",
            artifacts={"ideas": "idea list"},
        )

    async def fake_continue_after_ideation(run_uuid, approved: bool = True, comment=None):
        assert run_uuid == run_id
        assert approved is True
        calls["ideation"] += 1
        return WorkflowResult(
            run_id=str(run_id),
            status=RunStatus.AWAITING_PUBLISH_APPROVAL.value,
            current_step="editor",
            artifacts={"editor": "render plan"},
        )

    async def fake_continue_after_publish_approval(run_uuid, approved: bool = True, comment=None):
        assert run_uuid == run_id
        assert approved is True
        calls["publish"] += 1
        return WorkflowResult(
            run_id=str(run_id),
            status=RunStatus.COMPLETED.value,
            current_step="completed",
            artifacts={"published": "url"},
        )

    class FakeRunRepo:
        def __init__(self, _session):
            pass

    class FakeArtifactRepo:
        def __init__(self, _session):
            pass

    @contextmanager
    def fake_session():
        yield SimpleNamespace(expire_all=lambda: None)

    monkeypatch.setattr("myloware.cli.demo.RunRepository", FakeRunRepo)
    monkeypatch.setattr("myloware.cli.demo.ArtifactRepository", FakeArtifactRepo)
    monkeypatch.setattr("myloware.cli.demo.get_session", fake_session)
    monkeypatch.setattr("myloware.cli.demo.get_sync_client", lambda: object())
    monkeypatch.setattr("myloware.cli.demo.run_workflow", fake_run_workflow)
    monkeypatch.setattr(
        "myloware.workflows.langgraph.workflow.continue_after_ideation",
        fake_continue_after_ideation,
    )
    monkeypatch.setattr(
        "myloware.workflows.langgraph.workflow.continue_after_publish_approval",
        fake_continue_after_publish_approval,
    )

    # Inputs: brief prompt, then two confirmations (enter=Yes)
    result = CliRunner().invoke(cli, ["demo", "aismr"], input="demo brief\n\n\n")

    assert result.exit_code == 0
    assert calls["start"] == 1
    assert calls["ideation"] == 1
    assert calls["publish"] == 1
    assert RunStatus.COMPLETED.value in result.output


def test_kb_setup(monkeypatch):
    """kb setup loads docs and calls setup_project_knowledge."""

    calls: dict[str, object] = {}

    class FakeDoc:
        def __init__(self):
            self.id = "doc-1"
            self.content = "hello"
            self.metadata = {"source": "unit"}
            self.filename = "test.md"

    def fake_load_documents_with_manifest(project_id, include_global, read_content):
        assert include_global is True
        assert read_content is True
        calls["docs_project_id"] = project_id
        return [FakeDoc()], {"hash": "manifest123"}

    def fake_setup_project_knowledge(**kwargs):
        calls["kwargs"] = kwargs
        return "project_kb_global"

    monkeypatch.setattr("myloware.cli.kb.get_sync_client", lambda: object())
    monkeypatch.setattr(
        "myloware.knowledge.loader.load_documents_with_manifest", fake_load_documents_with_manifest
    )
    monkeypatch.setattr(
        "myloware.knowledge.setup.setup_project_knowledge", fake_setup_project_knowledge
    )

    result = CliRunner().invoke(
        cli,
        [
            "kb",
            "setup",
            "--project",
            "global",
            "--provider-id",
            "pgvector",
            "--embedding-model",
            "foo-model",
            "--embedding-dimension",
            "768",
            "--chunk-size",
            "256",
            "--chunk-overlap",
            "32",
        ],
    )

    assert result.exit_code == 0
    assert calls["docs_project_id"] is None
    assert "KB setup complete" in result.output

    kwargs = calls["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["project_id"] == "global"
    assert kwargs["provider_id"] == "pgvector"
    assert kwargs["embedding_model"] == "foo-model"
    assert kwargs["embedding_dimension"] == 768
    assert kwargs["chunk_size"] == 256
    assert kwargs["chunk_overlap"] == 32


def test_stack_shields_retrieve(monkeypatch):
    """stack shields <id> uses retrieve and prints the shield."""

    class FakeShields:
        def __init__(self):
            self.retrieved = None

        def retrieve(self, shield_id):
            self.retrieved = shield_id
            return SimpleNamespace(id=shield_id, name="safety-net")

        def list(self):
            return []

    fake_shields = FakeShields()
    fake_client = SimpleNamespace(shields=fake_shields)

    monkeypatch.setattr("myloware.cli.stack.get_sync_client", lambda: fake_client)

    result = CliRunner().invoke(cli, ["stack", "shields", "shield-1", "--json"])

    assert result.exit_code == 0
    assert "shield-1" in result.output
    assert fake_shields.retrieved == "shield-1"


def test_stack_vector_dbs_register(monkeypatch):
    """stack vector-dbs register calls vector_stores.create with provided params."""

    create_calls = {}

    class FakeVectorStores:
        def create(self, **kwargs):
            create_calls["kwargs"] = kwargs
            return SimpleNamespace(id="vs-1", name=kwargs.get("name"))

        def list(self):
            return []

    fake_client = SimpleNamespace(vector_stores=FakeVectorStores())

    monkeypatch.setattr("myloware.cli.stack.get_sync_client", lambda: fake_client)

    result = CliRunner().invoke(
        cli,
        [
            "stack",
            "vector-dbs",
            "register",
            "my-store",
            "--provider-id",
            "pgvector",
            "--embedding-model",
            "foo-model",
            "--embedding-dimension",
            "768",
            "--chunk-size",
            "256",
            "--chunk-overlap",
            "32",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"id": "vs-1"' in result.output
    assert create_calls["kwargs"]["name"] == "my-store"
    assert create_calls["kwargs"]["chunking_strategy"]["static"]["max_chunk_size_tokens"] == 256
    assert create_calls["kwargs"]["chunking_strategy"]["static"]["chunk_overlap_tokens"] == 32
    assert create_calls["kwargs"]["extra_body"]["provider_id"] == "pgvector"
    assert create_calls["kwargs"]["extra_body"]["embedding_model"] == "foo-model"
    assert create_calls["kwargs"]["extra_body"]["embedding_dimension"] == 768
