from __future__ import annotations

from dataclasses import dataclass
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from myloware.storage.models import ArtifactType, RunStatus
from myloware.workflows.langgraph import workflow as wf


@dataclass
class FakeArtifact:
    artifact_type: str
    uri: str | None = None
    artifact_metadata: dict[str, object] | None = None


@dataclass
class FakeRun:
    id: UUID
    workflow_name: str = "aismr"
    input: str | None = "brief"
    status: str = RunStatus.PENDING.value
    current_step: str | None = None
    error: str | None = None
    artifacts: dict[str, object] | None = None
    vector_db_id: str | None = "kb"


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def close(self) -> None:
        return None


class FakeAsyncSessionCM:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


class FakeRunRepo:
    def __init__(self, session: FakeSession, run: FakeRun | None) -> None:
        self.session = session
        self._run = run
        self.created: list[dict[str, object]] = []
        self.updated: list[tuple[UUID, dict[str, object]]] = []

    async def create_async(self, **kwargs):  # type: ignore[no-untyped-def]
        run = FakeRun(id=uuid4(), workflow_name=str(kwargs.get("workflow_name") or "aismr"))
        self._run = run
        self.created.append(kwargs)
        return run

    async def get_async(self, _run_id: UUID):  # type: ignore[no-untyped-def]
        return self._run

    async def update_async(self, run_id: UUID, **kwargs):  # type: ignore[no-untyped-def]
        self.updated.append((run_id, kwargs))
        if self._run and self._run.id == run_id:
            for k, v in kwargs.items():
                setattr(self._run, k, v)

    def get(self, _run_id: UUID):  # type: ignore[no-untyped-def]
        return self._run

    def create(self, **kwargs):  # type: ignore[no-untyped-def]
        run = FakeRun(id=uuid4(), workflow_name=str(kwargs.get("workflow_name") or "aismr"))
        self._run = run
        return run


class FakeArtifactRepo:
    def __init__(self, _session: FakeSession, artifacts: list[FakeArtifact]) -> None:
        self._artifacts = artifacts

    async def get_by_run_async(self, _run_id: UUID):  # type: ignore[no-untyped-def]
        return list(self._artifacts)


class FakeGraph:
    def __init__(self) -> None:
        self.invocations: list[dict[str, object]] = []
        self._state = SimpleNamespace(interrupts=[], values={})
        self._history: list[object] = []

    def set_state(self, interrupts: list[object], values: dict[str, object] | None = None) -> None:
        self._state = SimpleNamespace(interrupts=interrupts, values=values or {})

    def set_history(self, checkpoints: list[object]) -> None:
        self._history = checkpoints

    async def aget_state(self, _config):  # type: ignore[no-untyped-def]
        return self._state

    async def ainvoke(self, cmd_or_state, *, config, durability):  # type: ignore[no-untyped-def]
        self.invocations.append({"arg": cmd_or_state, "config": config, "durability": durability})
        # For start invocations, emulate returning state updates.
        if isinstance(cmd_or_state, dict):
            return cmd_or_state
        return {}

    async def aget_state_history(self, _config):  # type: ignore[no-untyped-def]
        for cp in self._history:
            yield cp

    async def aupdate_state(self, config, patch_values, *, as_node):  # type: ignore[no-untyped-def]
        # Return a new config used by subsequent calls.
        return {"configurable": {**config.get("configurable", {}), "checkpoint_id": "forked"}}


def _fake_async_session_factory(session: FakeSession):
    return lambda: FakeAsyncSessionCM(session)


@pytest.mark.asyncio
async def test_session_ctx_supports_async_sessionmaker_coroutine() -> None:
    session = FakeSession()

    async def factory():  # type: ignore[no-untyped-def]
        return FakeAsyncSessionCM(session)

    async with wf._session_ctx(lambda: factory()) as s:  # type: ignore[arg-type]
        assert s is session


@pytest.mark.asyncio
async def test_create_pending_run_commits_and_returns_id(monkeypatch) -> None:
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=None)

    monkeypatch.setattr("myloware.config.projects.load_project", lambda _name: object())

    run_id = await wf.create_pending_run(
        run_repo,
        workflow_name="aismr",
        brief="b",
        user_id="u",
        telegram_chat_id="t",
    )
    assert isinstance(run_id, UUID)
    assert session.commits == 1


@pytest.mark.asyncio
async def test_wait_for_run_visibility_returns_none_when_missing(monkeypatch) -> None:
    session = FakeSession()

    class Repo(FakeRunRepo):
        async def get_async(self, _run_id: UUID):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(wf, "RunRepository", lambda _s: Repo(session, run=None))

    out = await wf._wait_for_run_visibility(
        uuid4(),
        session_factory=_fake_async_session_factory(session),
        max_attempts=2,
        delay=0,
    )
    assert out is None


@pytest.mark.asyncio
async def test_run_workflow_async_handles_missing_run(monkeypatch) -> None:
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=None)
    graph = FakeGraph()

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    async def fake_wait(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(wf, "_wait_for_run_visibility", fake_wait)

    await wf.run_workflow_async(client=object(), run_id=uuid4(), vector_db_id="kb")
    assert run_repo.updated == []
    assert graph.invocations == []


@pytest.mark.asyncio
async def test_run_workflow_async_invokes_graph_and_marks_failed_on_exception(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run = FakeRun(id=run_id, workflow_name="aismr", input="brief", status=RunStatus.PENDING.value)
    run_repo = FakeRunRepo(session, run=run)

    class ExplodingGraph(FakeGraph):
        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    graph = ExplodingGraph()

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    async def fake_wait(_run_id, _sf):  # type: ignore[no-untyped-def]
        return run

    monkeypatch.setattr(wf, "_wait_for_run_visibility", fake_wait)

    await wf.run_workflow_async(client=object(), run_id=run_id, vector_db_id="kb")
    assert run_repo.updated
    assert run_repo.updated[-1][1]["status"] == RunStatus.FAILED.value
    assert session.commits >= 1


@pytest.mark.asyncio
async def test_continue_after_producer_requires_clips(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.FAILED.value))
    graph = FakeGraph()

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=[]))
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    out = await wf.continue_after_producer(run_id)
    assert out.status == RunStatus.FAILED.value
    assert out.error == "No video clips found"


@pytest.mark.asyncio
async def test_continue_after_producer_invokes_graph(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run = FakeRun(id=run_id, status=RunStatus.RUNNING.value, current_step="wait_for_videos")
    run_repo = FakeRunRepo(session, run=run)
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]

    class Intr:
        id = "intr-sora"
        value = {"waiting_for": "sora_webhook"}

    graph = FakeGraph()
    graph.set_state([Intr()])

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(
        wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=artifacts)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    out = await wf.continue_after_producer(run_id)
    assert out.run_id == str(run_id)
    assert graph.invocations


@pytest.mark.asyncio
async def test_continue_after_render_and_publish_require_matching_interrupt(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.FAILED.value))

    class Intr:
        id = "intr-remotion"
        value = {"waiting_for": "remotion_webhook"}

    graph = FakeGraph()
    graph.set_state([Intr()])

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    out = await wf.continue_after_render(run_id, "https://example.com/out.mp4")
    assert out.run_id == str(run_id)

    class IntrPub:
        id = "intr-pub"
        value = {"waiting_for": "publish_webhook"}

    graph.set_state([IntrPub()])
    out = await wf.continue_after_publish(run_id, ["https://example.com/p.mp4"])
    assert out.run_id == str(run_id)


@pytest.mark.asyncio
async def test_continue_after_publish_approval_handles_missing_run(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=None)

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)

    out = await wf.continue_after_publish_approval(run_id)
    assert out.status == RunStatus.FAILED.value
    assert out.error == "Run not found"


@pytest.mark.asyncio
async def test_fork_from_clips_happy_path(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.FAILED.value))
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]

    class Checkpoint:
        def __init__(self) -> None:
            self.config = {"configurable": {"thread_id": str(run_id), "checkpoint_id": "cp-1"}}
            self.values = {"current_step": "wait_for_videos"}
            self.interrupts = [
                SimpleNamespace(id="intr-sora", value={"waiting_for": "sora_webhook"})
            ]

    graph = FakeGraph()
    graph.set_history([Checkpoint()])
    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(
        wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=artifacts)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    out = await wf.fork_from_clips(run_id)
    assert out.run_id == str(run_id)
    assert graph.invocations


@pytest.mark.asyncio
async def test_fork_from_clips_raises_when_no_video_urls_and_initializes_checkpointer(
    monkeypatch,
) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.FAILED.value))
    graph = FakeGraph()

    init_called = {"count": 0}

    async def fake_init():  # type: ignore[no-untyped-def]
        init_called["count"] += 1

    monkeypatch.setattr(wf.settings, "database_url", "postgresql://localhost/db")
    monkeypatch.setattr(
        "myloware.workflows.langgraph.graph.ensure_checkpointer_initialized", fake_init
    )
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=[]))
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    with pytest.raises(ValueError, match="No VIDEO_CLIP artifacts found"):
        await wf.fork_from_clips(run_id)
    assert init_called["count"] == 1


@pytest.mark.asyncio
async def test_fork_from_clips_skips_non_matching_checkpoint_ids_and_reads_nested_state(
    monkeypatch,
) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.FAILED.value))
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]

    class CpOther:
        config = {"configurable": {"thread_id": str(run_id), "checkpoint_id": "cp-other"}}
        values = {"current_step": "wait_for_videos"}
        interrupts = [SimpleNamespace(id="intr-sora", value={"waiting_for": "sora_webhook"})]

    class CpTarget:
        config = {"configurable": {"thread_id": str(run_id), "checkpoint_id": "cp-target"}}
        values = {"state": {"current_step": "wait_for_videos"}}
        interrupts = [SimpleNamespace(id="intr-sora", value={"waiting_for": "sora_webhook"})]

    graph = FakeGraph()
    graph.set_history([CpOther(), CpTarget()])
    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(
        wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=artifacts)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    out = await wf.fork_from_clips(run_id, checkpoint_id="cp-target")
    assert out.run_id == str(run_id)
    assert graph.invocations


@pytest.mark.asyncio
async def test_fork_from_clips_rejects_non_wait_checkpoint_when_selected(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.FAILED.value))
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]

    class Cp:
        config = {"configurable": {"thread_id": str(run_id), "checkpoint_id": "cp-1"}}
        values = {"current_step": "ideation"}
        interrupts: list[object] = []

    graph = FakeGraph()
    graph.set_history([Cp()])

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(
        wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=artifacts)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    with pytest.raises(ValueError, match="not a wait_for_videos"):
        await wf.fork_from_clips(run_id, checkpoint_id="cp-1")


@pytest.mark.asyncio
async def test_fork_from_clips_requires_sora_wait_checkpoint_when_auto_selecting(
    monkeypatch,
) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.FAILED.value))
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]

    class Cp:
        config = {"configurable": {"thread_id": str(run_id), "checkpoint_id": "cp-1"}}
        values = {"current_step": "ideation"}
        interrupts: list[object] = []

    graph = FakeGraph()
    graph.set_history([Cp()])

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(
        wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=artifacts)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    with pytest.raises(ValueError, match="No sora_webhook wait checkpoint"):
        await wf.fork_from_clips(run_id)


@pytest.mark.asyncio
async def test_fork_from_clips_rejects_selected_checkpoint_missing_config(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.FAILED.value))
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]

    class Cp:
        config = None
        values = {"current_step": "wait_for_videos"}
        interrupts = [SimpleNamespace(id="intr-sora", value={"waiting_for": "sora_webhook"})]

    graph = FakeGraph()
    graph.set_history([Cp()])

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(
        wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=artifacts)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    with pytest.raises(ValueError, match="Selected checkpoint missing config"):
        await wf.fork_from_clips(run_id)


@pytest.mark.asyncio
async def test_fork_from_clips_requires_sora_interrupt_on_selected_checkpoint(
    monkeypatch,
) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.FAILED.value))
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]

    class Cp:
        config = {"configurable": {"thread_id": str(run_id), "checkpoint_id": "cp-1"}}
        values = {"current_step": "wait_for_videos"}
        interrupts: list[object] = []

    graph = FakeGraph()
    graph.set_history([Cp()])

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(
        wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=artifacts)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    with pytest.raises(ValueError, match="Selected checkpoint has no sora_webhook interrupt"):
        await wf.fork_from_clips(run_id, checkpoint_id="cp-1")


def test_session_ctx_returns_existing_context_manager() -> None:
    session = FakeSession()
    cm = FakeAsyncSessionCM(session)
    assert wf._session_ctx(cm) is cm


def test_session_ctx_falls_back_to_session_object() -> None:
    obj = object()
    assert wf._session_ctx(obj) is obj


@pytest.mark.asyncio
async def test_wait_for_run_visibility_returns_run_when_found(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id))
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)

    out = await wf._wait_for_run_visibility(
        run_id,
        session_factory=_fake_async_session_factory(session),
        max_attempts=1,
        delay=0,
    )
    assert out is run_repo._run


@pytest.mark.asyncio
async def test_run_workflow_async_success_calls_checkpointer_and_logs_completion(
    monkeypatch,
) -> None:
    run_id = uuid4()
    session = FakeSession()
    run = FakeRun(id=run_id, workflow_name="aismr", input="brief", status=RunStatus.PENDING.value)
    run_repo = FakeRunRepo(session, run=run)
    graph = FakeGraph()

    init_called = {"count": 0}

    async def fake_init():  # type: ignore[no-untyped-def]
        init_called["count"] += 1

    async def fake_wait(*_a, **_kw):  # type: ignore[no-untyped-def]
        return run

    monkeypatch.setattr(wf.settings, "database_url", "postgresql://localhost/db")
    monkeypatch.setattr(
        "myloware.workflows.langgraph.graph.ensure_checkpointer_initialized", fake_init
    )
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)
    monkeypatch.setattr(wf, "_wait_for_run_visibility", fake_wait)

    await wf.run_workflow_async(client=object(), run_id=run_id, vector_db_id="kb")
    assert init_called["count"] == 1
    assert graph.invocations


def test_run_workflow_sync_wrapper_returns_final_run(monkeypatch) -> None:
    stored: dict[str, object] = {"run": None}

    class FakeSyncSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def commit(self):
            return None

    def get_session_factory():  # type: ignore[no-untyped-def]
        return lambda: FakeSyncSession()

    class FakeSyncRunRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            pass

        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            run = FakeRun(
                id=uuid4(),
                workflow_name=str(kwargs.get("workflow_name") or "aismr"),
                input=str(kwargs.get("input") or ""),
                status=RunStatus.PENDING.value,
                current_step="ideation",
            )
            stored["run"] = run
            return run

        def get(self, run_id: UUID):  # type: ignore[no-untyped-def]
            run = stored["run"]
            if isinstance(run, FakeRun) and run.id == run_id:
                return run
            return None

    async def fake_run_workflow_async(*, client, run_id: UUID, vector_db_id: str, notifier=None):  # type: ignore[no-untyped-def,ARG001]
        run = stored["run"]
        if isinstance(run, FakeRun) and run.id == run_id:
            run.status = RunStatus.COMPLETED.value
            run.current_step = "done"

    monkeypatch.setattr("myloware.storage.database.get_session_factory", get_session_factory)
    monkeypatch.setattr(wf, "RunRepository", FakeSyncRunRepo)
    monkeypatch.setattr(wf, "run_workflow_async", fake_run_workflow_async)

    out = wf.run_workflow(
        client=object(),
        brief="b",
        vector_db_id="kb",
        run_repo=SimpleNamespace(),
        artifact_repo=SimpleNamespace(),
        workflow_name="aismr",
    )
    assert out.status == RunStatus.COMPLETED.value
    assert out.current_step == "done"


def test_run_workflow_sync_wrapper_handles_missing_run_after_execution(monkeypatch) -> None:
    class FakeSyncSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def commit(self):
            return None

    def get_session_factory():  # type: ignore[no-untyped-def]
        return lambda: FakeSyncSession()

    class FakeSyncRunRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            pass

        def create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return FakeRun(id=uuid4())

        def get(self, _run_id: UUID):  # type: ignore[no-untyped-def]
            return None

    async def fake_run_workflow_async(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("myloware.storage.database.get_session_factory", get_session_factory)
    monkeypatch.setattr(wf, "RunRepository", FakeSyncRunRepo)
    monkeypatch.setattr(wf, "run_workflow_async", fake_run_workflow_async)

    out = wf.run_workflow(
        client=object(),
        brief="b",
        vector_db_id="kb",
        run_repo=SimpleNamespace(),
        artifact_repo=SimpleNamespace(),
        workflow_name="aismr",
    )
    assert out.status == RunStatus.FAILED.value
    assert out.error == "Run not found after execution"


@pytest.mark.asyncio
async def test_continue_after_ideation_returns_failed_for_missing_run(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=None)

    monkeypatch.setattr(
        "myloware.storage.database.get_async_session_factory",
        lambda: _fake_async_session_factory(session),
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)

    out = await wf.continue_after_ideation(run_id)
    assert out.status == RunStatus.FAILED.value
    assert out.error == "Run not found"


@pytest.mark.asyncio
async def test_continue_after_ideation_targets_interrupt_and_includes_comment(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run = FakeRun(id=run_id, status=RunStatus.RUNNING.value, current_step="ideation_approval")
    run_repo = FakeRunRepo(session, run=run)

    class Intr:
        interrupt_id = "intr-idea"
        value = {}

    graph = FakeGraph()
    graph.set_state([Intr()])

    init_called = {"count": 0}

    async def fake_init():  # type: ignore[no-untyped-def]
        init_called["count"] += 1

    monkeypatch.setattr(wf.settings, "database_url", "postgresql://localhost/db")
    monkeypatch.setattr(
        "myloware.workflows.langgraph.graph.ensure_checkpointer_initialized", fake_init
    )
    monkeypatch.setattr(
        "myloware.storage.database.get_async_session_factory",
        lambda: _fake_async_session_factory(session),
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    out = await wf.continue_after_ideation(run_id, approved=True, comment="lgtm")
    assert out.run_id == str(run_id)
    assert init_called["count"] == 1
    assert graph.invocations


@pytest.mark.asyncio
async def test_continue_after_producer_calls_checkpointer_when_non_sqlite(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run = FakeRun(id=run_id, status=RunStatus.RUNNING.value)
    run_repo = FakeRunRepo(session, run=run)
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]

    class Intr:
        id = "intr-sora"
        value = {"waiting_for": "sora_webhook"}

    graph = FakeGraph()
    graph.set_state([Intr()])

    init_called = {"count": 0}

    async def fake_init():  # type: ignore[no-untyped-def]
        init_called["count"] += 1

    monkeypatch.setattr(wf.settings, "database_url", "postgresql://localhost/db")
    monkeypatch.setattr(
        "myloware.workflows.langgraph.graph.ensure_checkpointer_initialized", fake_init
    )
    monkeypatch.setattr(
        "myloware.storage.database.get_async_session_factory",
        lambda: _fake_async_session_factory(session),
    )
    monkeypatch.setattr(
        wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=artifacts)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    out = await wf.continue_after_producer(run_id)
    assert out.run_id == str(run_id)
    assert init_called["count"] == 1


@pytest.mark.asyncio
async def test_continue_after_producer_raises_when_interrupt_missing(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]

    class Intr:
        id = "intr-other"
        value = {"waiting_for": "other"}

    graph = FakeGraph()
    graph.set_state([Intr()])

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        "myloware.storage.database.get_async_session_factory",
        lambda: _fake_async_session_factory(session),
    )
    monkeypatch.setattr(
        wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=artifacts)
    )
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    with pytest.raises(ValueError, match="No sora_webhook interrupt found"):
        await wf.continue_after_producer(run_id)


@pytest.mark.asyncio
async def test_continue_after_render_calls_checkpointer_when_non_sqlite(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.RUNNING.value))

    class Intr:
        id = "intr-remotion"
        value = {"waiting_for": "remotion_webhook"}

    graph = FakeGraph()
    graph.set_state([Intr()])

    init_called = {"count": 0}

    async def fake_init():  # type: ignore[no-untyped-def]
        init_called["count"] += 1

    monkeypatch.setattr(wf.settings, "database_url", "postgresql://localhost/db")
    monkeypatch.setattr(
        "myloware.workflows.langgraph.graph.ensure_checkpointer_initialized", fake_init
    )
    monkeypatch.setattr(
        "myloware.storage.database.get_async_session_factory",
        lambda: _fake_async_session_factory(session),
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    out = await wf.continue_after_render(run_id, "https://example.com/out.mp4")
    assert out.run_id == str(run_id)
    assert init_called["count"] == 1


@pytest.mark.asyncio
async def test_continue_after_render_raises_when_interrupt_missing(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.RUNNING.value))

    graph = FakeGraph()
    graph.set_state([])

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        "myloware.storage.database.get_async_session_factory",
        lambda: _fake_async_session_factory(session),
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    with pytest.raises(ValueError, match="No remotion_webhook interrupt found"):
        await wf.continue_after_render(run_id, "https://example.com/out.mp4")


@pytest.mark.asyncio
async def test_continue_after_publish_approval_invokes_graph_with_interrupt_id(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run = FakeRun(id=run_id, status=RunStatus.RUNNING.value, current_step="publish_approval")
    run_repo = FakeRunRepo(session, run=run)

    class Intr:
        interrupt_id = "intr-approve"
        value = {}

    graph = FakeGraph()
    graph.set_state([Intr()])

    init_called = {"count": 0}

    async def fake_init():  # type: ignore[no-untyped-def]
        init_called["count"] += 1

    monkeypatch.setattr(wf.settings, "database_url", "postgresql://localhost/db")
    monkeypatch.setattr(
        "myloware.workflows.langgraph.graph.ensure_checkpointer_initialized", fake_init
    )
    monkeypatch.setattr(
        "myloware.storage.database.get_async_session_factory",
        lambda: _fake_async_session_factory(session),
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    out = await wf.continue_after_publish_approval(run_id, approved=True, comment="ship it")
    assert out.run_id == str(run_id)
    assert init_called["count"] == 1
    assert graph.invocations


@pytest.mark.asyncio
async def test_continue_after_publish_approval_works_without_interrupt_id_when_state_fetch_fails(
    monkeypatch,
) -> None:
    run_id = uuid4()
    session = FakeSession()
    run = FakeRun(id=run_id, status=RunStatus.RUNNING.value, current_step="publish_approval")
    run_repo = FakeRunRepo(session, run=run)

    class ExplodingGraph(FakeGraph):
        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    graph = ExplodingGraph()

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        "myloware.storage.database.get_async_session_factory",
        lambda: _fake_async_session_factory(session),
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    out = await wf.continue_after_publish_approval(run_id, approved=False)
    assert out.run_id == str(run_id)
    assert graph.invocations


@pytest.mark.asyncio
async def test_continue_after_publish_calls_checkpointer_when_non_sqlite(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.RUNNING.value))

    class Intr:
        id = "intr-pub"
        value = {"waiting_for": "publish_webhook"}

    graph = FakeGraph()
    graph.set_state([Intr()])

    init_called = {"count": 0}

    async def fake_init():  # type: ignore[no-untyped-def]
        init_called["count"] += 1

    monkeypatch.setattr(wf.settings, "database_url", "postgresql://localhost/db")
    monkeypatch.setattr(
        "myloware.workflows.langgraph.graph.ensure_checkpointer_initialized", fake_init
    )
    monkeypatch.setattr(
        "myloware.storage.database.get_async_session_factory",
        lambda: _fake_async_session_factory(session),
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    out = await wf.continue_after_publish(run_id, ["https://example.com/p.mp4"])
    assert out.run_id == str(run_id)
    assert init_called["count"] == 1


@pytest.mark.asyncio
async def test_continue_after_publish_raises_when_interrupt_missing(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run_repo = FakeRunRepo(session, run=FakeRun(id=run_id, status=RunStatus.RUNNING.value))
    graph = FakeGraph()
    graph.set_state([])

    monkeypatch.setattr(wf.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        "myloware.storage.database.get_async_session_factory",
        lambda: _fake_async_session_factory(session),
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "get_graph", lambda: graph)

    with pytest.raises(ValueError, match="No publish_webhook interrupt found"):
        await wf.continue_after_publish(run_id, ["https://example.com/p.mp4"])


@pytest.mark.asyncio
async def test_repair_sora_clips_rejects_unsafe_run_status(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()

    called = {"for_update": 0}

    class Repo(FakeRunRepo):
        async def get_for_update_async(self, _run_id: UUID):  # type: ignore[no-untyped-def]
            called["for_update"] += 1
            return self._run

        async def get_async(self, _run_id: UUID):  # type: ignore[no-untyped-def]
            raise AssertionError("repair_sora_clips must lock via get_for_update_async")

    run_repo = Repo(session, run=FakeRun(id=run_id, status=RunStatus.RUNNING.value))

    class ArtifactRepo(FakeArtifactRepo):
        async def get_by_run_async(self, _run_id: UUID):  # type: ignore[no-untyped-def]
            raise AssertionError("Should not fetch artifacts for unsafe run status")

    monkeypatch.setattr(wf.settings, "sora_provider", "real")
    monkeypatch.setattr(wf.settings, "use_fake_providers", False)
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "ArtifactRepository", lambda _s: ArtifactRepo(_s, artifacts=[]))

    with pytest.raises(
        ValueError, match="only allowed when a run is awaiting video generation or failed"
    ):
        await wf.repair_sora_clips(run_id)
    assert called["for_update"] == 1


@pytest.mark.asyncio
async def test_repair_sora_clips_allows_failed_or_awaiting(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()

    class Repo(FakeRunRepo):
        async def get_for_update_async(self, _run_id: UUID):  # type: ignore[no-untyped-def]
            return self._run

    run_repo = Repo(
        session, run=FakeRun(id=run_id, status=RunStatus.AWAITING_VIDEO_GENERATION.value)
    )

    monkeypatch.setattr(wf.settings, "sora_provider", "real")
    monkeypatch.setattr(wf.settings, "use_fake_providers", False)
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=[]))

    with pytest.raises(ValueError, match="No clip_manifest found"):
        await wf.repair_sora_clips(run_id)


@pytest.mark.asyncio
async def test_repair_sora_clips_happy_path(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run = FakeRun(
        id=run_id,
        status=RunStatus.AWAITING_VIDEO_GENERATION.value,
        current_step="wait_for_videos",
        artifacts={},
    )

    class Repo(FakeRunRepo):
        async def get_for_update_async(self, _run_id: UUID):  # type: ignore[no-untyped-def]
            return self._run

        async def add_artifact_async(self, _run_id: UUID, key: str, value: object):  # type: ignore[no-untyped-def]
            artifacts = dict(self._run.artifacts or {})
            artifacts[key] = value
            self._run.artifacts = artifacts

    run_repo = Repo(session, run=run)

    clip_manifest = SimpleNamespace(
        artifact_type=ArtifactType.CLIP_MANIFEST.value,
        artifact_metadata={"type": "task_metadata_mapping", "task_count": 2},
        content="{}",
    )
    sora_request_payload = {
        "videos": [
            {"video_index": 1, "prompt": "new clip"},
        ],
        "aspect_ratio": "9:16",
        "n_frames": "8",
        "remove_watermark": True,
    }
    sora_request = SimpleNamespace(
        artifact_type=ArtifactType.SORA_REQUEST.value,
        artifact_metadata={},
        content=json.dumps(sora_request_payload),
    )
    existing_clip = SimpleNamespace(
        artifact_type=ArtifactType.VIDEO_CLIP.value,
        uri="https://example.com/clip0.mp4",
        artifact_metadata={"video_index": "0", "task_id": "task-old", "topic": "t0"},
    )

    class ArtifactRepo(FakeArtifactRepo):
        def __init__(self, _session: FakeSession, artifacts: list[object]) -> None:
            super().__init__(_session, artifacts=artifacts)  # type: ignore[arg-type]
            self.created: list[dict[str, object]] = []

        async def create_async(self, **kwargs):  # type: ignore[no-untyped-def]
            self.created.append(kwargs)

    artifact_repo = ArtifactRepo(session, [clip_manifest, sora_request, existing_clip])

    async def fake_submit(self, videos, aspect_ratio, n_frames, workflow_name=None):  # type: ignore[no-untyped-def]
        assert aspect_ratio == "9:16"
        assert n_frames == "8"
        assert videos[0]["video_index"] == 1
        return ["task-new"], {"task-new": {"video_index": 1, "topic": "t1"}}, None

    monkeypatch.setattr(wf.settings, "sora_provider", "real")
    # Submission is stubbed below; constructor validation still requires a test key.
    monkeypatch.setattr(wf.settings, "openai_api_key", "unit-test-key")
    monkeypatch.setattr(wf.settings, "use_fake_providers", False)
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "ArtifactRepository", lambda _s: artifact_repo)
    monkeypatch.setattr("myloware.tools.sora.SoraGenerationTool._submit_openai_videos", fake_submit)

    out = await wf.repair_sora_clips(run_id, video_indexes=[1])
    assert out.run_id == str(run_id)
    assert out.status == RunStatus.AWAITING_VIDEO_GENERATION.value
    assert out.current_step == "wait_for_videos"
    assert run.artifacts["pending_task_ids"] == ["task-old", "task-new"]
    assert run.artifacts["repair_resubmitted_video_indexes"] == [1]
    assert artifact_repo.created
    created_meta = artifact_repo.created[0]["metadata"]
    assert created_meta["repair"] is True


@pytest.mark.asyncio
async def test_repair_sora_clips_returns_when_no_missing(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()
    run = FakeRun(
        id=run_id,
        status=RunStatus.AWAITING_VIDEO_GENERATION.value,
        current_step="wait_for_videos",
    )

    class Repo(FakeRunRepo):
        async def get_for_update_async(self, _run_id: UUID):  # type: ignore[no-untyped-def]
            return self._run

    run_repo = Repo(session, run=run)

    clip_manifest = SimpleNamespace(
        artifact_type=ArtifactType.CLIP_MANIFEST.value,
        artifact_metadata={"type": "task_metadata_mapping", "task_count": 1},
        content="{}",
    )
    existing_clip = SimpleNamespace(
        artifact_type=ArtifactType.VIDEO_CLIP.value,
        uri="https://example.com/clip0.mp4",
        artifact_metadata={"video_index": "0", "task_id": "task-old"},
    )

    artifact_repo = FakeArtifactRepo(session, [clip_manifest, existing_clip])

    monkeypatch.setattr(wf.settings, "sora_provider", "real")
    monkeypatch.setattr(wf.settings, "use_fake_providers", False)
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "ArtifactRepository", lambda _s: artifact_repo)

    out = await wf.repair_sora_clips(run_id)
    assert out.error == "No missing clips to repair"


@pytest.mark.asyncio
async def test_repair_sora_clips_requires_real_provider(monkeypatch) -> None:
    run_id = uuid4()

    monkeypatch.setattr(wf.settings, "sora_provider", "fake")
    monkeypatch.setattr(wf.settings, "use_fake_providers", False)

    with pytest.raises(ValueError, match="requires SORA_PROVIDER=real"):
        await wf.repair_sora_clips(run_id)


@pytest.mark.asyncio
async def test_repair_sora_clips_run_not_found(monkeypatch) -> None:
    run_id = uuid4()
    session = FakeSession()

    class Repo(FakeRunRepo):
        async def get_for_update_async(self, _run_id: UUID):  # type: ignore[no-untyped-def]
            return None

    run_repo = Repo(session, run=None)

    monkeypatch.setattr(wf.settings, "sora_provider", "real")
    monkeypatch.setattr(wf.settings, "use_fake_providers", False)
    monkeypatch.setattr(
        wf, "get_async_session_factory", lambda: _fake_async_session_factory(session)
    )
    monkeypatch.setattr(wf, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(wf, "ArtifactRepository", lambda _s: FakeArtifactRepo(_s, artifacts=[]))

    with pytest.raises(ValueError, match="not found"):
        await wf.repair_sora_clips(run_id)
