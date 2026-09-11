from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from myloware.storage.models import ArtifactType, RunStatus
from myloware.workflows.langgraph import resume as resume_mod


@dataclass
class FakeArtifact:
    artifact_type: str
    uri: str | None = None
    artifact_metadata: dict[str, object] | None = None
    content: str | None = None


class FakeArtifactRepo:
    def __init__(self, artifacts: list[FakeArtifact]) -> None:
        self._artifacts = artifacts

    async def get_by_run_async(self, _run_id: UUID):  # type: ignore[no-untyped-def]
        return list(self._artifacts)


class FakeRunRepo:
    def __init__(self, workflow_name: str | None = "motivational") -> None:
        self.workflow_name = workflow_name
        self.updates: list[tuple[UUID, dict[str, object]]] = []

    async def get_async(self, run_id: UUID):  # type: ignore[no-untyped-def]
        if self.workflow_name is None:
            return None
        return SimpleNamespace(id=run_id, workflow_name=self.workflow_name)

    async def update_async(self, run_id: UUID, **kwargs):  # type: ignore[no-untyped-def]
        self.updates.append((run_id, kwargs))


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class FakeSessionCM:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


@pytest.mark.asyncio
async def test_resume_after_videos_invokes_graph(monkeypatch) -> None:
    run_id = uuid4()
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]
    artifact_repo = FakeArtifactRepo(artifacts)
    run_repo = FakeRunRepo()
    session = FakeSession()

    class FakeInterrupt:
        id = "intr-sora"
        value = {"waiting_for": "sora_webhook"}

    class FakeGraph:
        def __init__(self) -> None:
            self.invoked = 0

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                interrupts=[FakeInterrupt()], values={"current_step": "wait_for_videos"}
            )

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            self.invoked += 1
            return None

    graph = FakeGraph()

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: graph)
    monkeypatch.setattr(resume_mod, "ArtifactRepository", lambda _s: artifact_repo)
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    await resume_mod.resume_after_videos(run_id)

    assert graph.invoked == 1
    assert run_repo.updates == []


@pytest.mark.asyncio
async def test_resume_after_videos_rejects_monthly_before_graph_or_run_mutation(
    monkeypatch,
) -> None:
    from myloware.workflows.admission import StudioAdmissionRequiredError

    run_id = uuid4()
    run_repo = FakeRunRepo(workflow_name="monthly")
    session = FakeSession()

    class FakeGraph:
        invoked = 0

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            FakeGraph.invoked += 1
            raise AssertionError("monthly guard must run before graph state reads")

    monkeypatch.setenv("AISMR_ENABLED", "true")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    with pytest.raises(StudioAdmissionRequiredError, match="studio_admission_required"):
        await resume_mod.resume_after_videos(run_id, raise_on_error=True)

    assert FakeGraph.invoked == 0
    assert run_repo.updates == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_resume_after_render_rejects_monthly_before_graph_or_run_mutation(
    monkeypatch,
) -> None:
    from myloware.workflows.admission import StudioAdmissionRequiredError

    run_id = uuid4()
    run_repo = FakeRunRepo(workflow_name="monthly")
    session = FakeSession()

    class FakeGraph:
        invoked = 0

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            FakeGraph.invoked += 1
            raise AssertionError("monthly guard must run before graph state reads")

    monkeypatch.setenv("AISMR_ENABLED", "true")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    with pytest.raises(StudioAdmissionRequiredError, match="studio_admission_required"):
        await resume_mod.resume_after_render(
            run_id, "https://example.com/out.mp4", raise_on_error=True
        )

    assert FakeGraph.invoked == 0
    assert run_repo.updates == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_resume_after_videos_marks_failed_on_no_clips(monkeypatch) -> None:
    run_id = uuid4()
    artifact_repo = FakeArtifactRepo([])
    run_repo = FakeRunRepo()
    session = FakeSession()

    class FakeGraph:
        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(interrupts=[], values={})

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            raise AssertionError("should not invoke when clips are missing")

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "ArtifactRepository", lambda _s: artifact_repo)
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    await resume_mod.resume_after_videos(run_id)

    assert run_repo.updates
    assert run_repo.updates[-1][1]["status"] == RunStatus.FAILED.value
    assert session.commits == 1


@pytest.mark.asyncio
async def test_resume_after_videos_returns_early_when_terminal(monkeypatch) -> None:
    run_id = uuid4()
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]
    artifact_repo = FakeArtifactRepo(artifacts)
    run_repo = FakeRunRepo()
    session = FakeSession()

    class FakeGraph:
        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(interrupts=[], values={"status": RunStatus.COMPLETED.value})

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            raise AssertionError("should not invoke after terminal state")

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "ArtifactRepository", lambda _s: artifact_repo)
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    await resume_mod.resume_after_videos(run_id)
    assert run_repo.updates == []


@pytest.mark.asyncio
async def test_resume_after_videos_times_out_marks_failed_and_raises(monkeypatch) -> None:
    run_id = uuid4()
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]
    artifact_repo = FakeArtifactRepo(artifacts)
    run_repo = FakeRunRepo()
    session = FakeSession()

    class FakeGraph:
        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            # No interrupts; keep waiting until timeout.
            return SimpleNamespace(interrupts=[], values={"current_step": "wait_for_videos"})

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            raise AssertionError("should not invoke without interrupt id")

    # Force immediate timeout by faking monotonic time.
    t = {"now": 0.0}

    def fake_monotonic() -> float:
        t["now"] += 10.0
        return t["now"]

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "ArtifactRepository", lambda _s: artifact_repo)
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )
    monkeypatch.setattr(resume_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(resume_mod.anyio, "sleep", fake_sleep)

    with pytest.raises(resume_mod.ResumeRetryableError, match="No sora_webhook interrupt found"):
        await resume_mod.resume_after_videos(run_id, raise_on_error=True)

    assert run_repo.updates == []


@pytest.mark.asyncio
async def test_resume_after_render_requires_video_url() -> None:
    with pytest.raises(ValueError, match="video_url is required"):
        await resume_mod.resume_after_render(uuid4(), "")


@pytest.mark.asyncio
async def test_resume_after_render_invokes_graph(monkeypatch) -> None:
    run_id = uuid4()
    run_repo = FakeRunRepo()
    session = FakeSession()

    class FakeInterrupt:
        id = "intr-remotion"
        value = {"waiting_for": "remotion_webhook"}

    class FakeGraph:
        invoked = 0

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                interrupts=[FakeInterrupt()], values={"current_step": "wait_for_render"}
            )

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            FakeGraph.invoked += 1
            return None

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    await resume_mod.resume_after_render(run_id, "https://example.com/out.mp4")
    assert FakeGraph.invoked == 1
    assert run_repo.updates == []


@pytest.mark.asyncio
async def test_resume_after_render_returns_early_when_terminal(monkeypatch) -> None:
    run_id = uuid4()
    run_repo = FakeRunRepo()
    session = FakeSession()

    class FakeGraph:
        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(interrupts=[], values={"status": RunStatus.COMPLETED.value})

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            raise AssertionError("should not invoke after terminal state")

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    await resume_mod.resume_after_render(run_id, "https://example.com/out.mp4")
    assert run_repo.updates == []


@pytest.mark.asyncio
async def test_resume_after_render_times_out_marks_failed(monkeypatch) -> None:
    run_id = uuid4()
    run_repo = FakeRunRepo()
    session = FakeSession()

    class FakeGraph:
        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(interrupts=[], values={"current_step": "wait_for_render"})

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            raise AssertionError("should not invoke without interrupt id")

    t = {"now": 0.0}

    def fake_monotonic() -> float:
        t["now"] += 10.0
        return t["now"]

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )
    monkeypatch.setattr(resume_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(resume_mod.anyio, "sleep", fake_sleep)

    monkeypatch.setattr(resume_mod.settings, "workflow_dispatcher", "inprocess")
    await resume_mod.resume_after_render(run_id, "https://example.com/out.mp4")
    assert run_repo.updates == []


@pytest.mark.asyncio
async def test_resume_after_videos_calls_checkpointer_for_non_sqlite(monkeypatch) -> None:
    run_id = uuid4()
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]
    artifact_repo = FakeArtifactRepo(artifacts)
    run_repo = FakeRunRepo()
    session = FakeSession()

    ensure = AsyncMock()

    class FakeInterrupt:
        id = "intr-sora"
        value = {"waiting_for": "sora_webhook"}

    class FakeGraph:
        invoked = 0

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                interrupts=[FakeInterrupt()], values={"current_step": "wait_for_videos"}
            )

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            FakeGraph.invoked += 1
            return None

    monkeypatch.setattr(resume_mod.settings, "database_url", "postgresql://example")
    monkeypatch.setattr(resume_mod, "ensure_checkpointer_initialized", ensure)
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "ArtifactRepository", lambda _s: artifact_repo)
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    await resume_mod.resume_after_videos(run_id)
    ensure.assert_awaited_once()
    assert FakeGraph.invoked == 1


@pytest.mark.asyncio
async def test_resume_after_videos_returns_early_when_step_is_past_wait_for_videos(
    monkeypatch,
) -> None:
    run_id = uuid4()
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]
    artifact_repo = FakeArtifactRepo(artifacts)
    run_repo = FakeRunRepo()
    session = FakeSession()

    class FakeGraph:
        invoked = 0

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                interrupts=[], values={"current_step": "editing", "status": RunStatus.RUNNING.value}
            )

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            FakeGraph.invoked += 1
            return None

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "ArtifactRepository", lambda _s: artifact_repo)
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    await resume_mod.resume_after_videos(run_id)
    assert FakeGraph.invoked == 0
    assert run_repo.updates == []


@pytest.mark.asyncio
async def test_resume_after_videos_sleeps_until_interrupt_found(monkeypatch) -> None:
    run_id = uuid4()
    artifacts = [
        FakeArtifact(artifact_type=ArtifactType.VIDEO_CLIP.value, uri="https://example.com/a.mp4")
    ]
    artifact_repo = FakeArtifactRepo(artifacts)
    run_repo = FakeRunRepo()
    session = FakeSession()

    sleep = AsyncMock()

    class FakeInterrupt:
        id = "intr-sora"
        value = {"waiting_for": "sora_webhook"}

    class FakeGraph:
        invoked = 0

        def __init__(self) -> None:
            self._calls = 0

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            self._calls += 1
            if self._calls == 1:
                return SimpleNamespace(interrupts=[], values={"current_step": "wait_for_videos"})
            return SimpleNamespace(
                interrupts=[FakeInterrupt()], values={"current_step": "wait_for_videos"}
            )

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            FakeGraph.invoked += 1
            return None

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod.anyio, "sleep", sleep)
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "ArtifactRepository", lambda _s: artifact_repo)
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    await resume_mod.resume_after_videos(run_id)
    sleep.assert_awaited()


@pytest.mark.asyncio
async def test_resume_after_render_calls_checkpointer_for_non_sqlite(monkeypatch) -> None:
    run_id = uuid4()
    run_repo = FakeRunRepo()
    session = FakeSession()

    ensure = AsyncMock()

    class FakeInterrupt:
        id = "intr-remotion"
        value = {"waiting_for": "remotion_webhook"}

    class FakeGraph:
        invoked = 0

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                interrupts=[FakeInterrupt()], values={"current_step": "wait_for_render"}
            )

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            FakeGraph.invoked += 1
            return None

    monkeypatch.setattr(resume_mod.settings, "database_url", "postgresql://example")
    monkeypatch.setattr(resume_mod, "ensure_checkpointer_initialized", ensure)
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    await resume_mod.resume_after_render(run_id, "https://example.com/out.mp4")
    ensure.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_after_render_returns_early_when_step_is_past_wait_for_render(
    monkeypatch,
) -> None:
    run_id = uuid4()
    run_repo = FakeRunRepo()
    session = FakeSession()

    class FakeGraph:
        invoked = 0

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                interrupts=[],
                values={"current_step": "publishing", "status": RunStatus.RUNNING.value},
            )

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            FakeGraph.invoked += 1
            return None

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    await resume_mod.resume_after_render(run_id, "https://example.com/out.mp4")
    assert FakeGraph.invoked == 0
    assert run_repo.updates == []


@pytest.mark.asyncio
async def test_resume_after_render_sleeps_until_interrupt_found(monkeypatch) -> None:
    run_id = uuid4()
    run_repo = FakeRunRepo()
    session = FakeSession()

    sleep = AsyncMock()

    class FakeInterrupt:
        id = "intr-remotion"
        value = {"waiting_for": "remotion_webhook"}

    class FakeGraph:
        invoked = 0

        def __init__(self) -> None:
            self._calls = 0

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            self._calls += 1
            if self._calls == 1:
                return SimpleNamespace(interrupts=[], values={"current_step": "wait_for_render"})
            return SimpleNamespace(
                interrupts=[FakeInterrupt()], values={"current_step": "wait_for_render"}
            )

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            FakeGraph.invoked += 1
            return None

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod.anyio, "sleep", sleep)
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )

    await resume_mod.resume_after_render(run_id, "https://example.com/out.mp4")
    sleep.assert_awaited()


@pytest.mark.asyncio
async def test_resume_after_render_times_out_marks_failed_and_raises(monkeypatch) -> None:
    run_id = uuid4()
    run_repo = FakeRunRepo()
    session = FakeSession()

    class FakeGraph:
        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(interrupts=[], values={"current_step": "wait_for_render"})

        async def ainvoke(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            raise AssertionError("should not invoke without interrupt id")

    t = {"now": 0.0}

    def fake_monotonic() -> float:
        t["now"] += 10.0
        return t["now"]

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(resume_mod.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(resume_mod, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(resume_mod, "RunRepository", lambda _s: run_repo)
    monkeypatch.setattr(
        resume_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM(session))
    )
    monkeypatch.setattr(resume_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(resume_mod.anyio, "sleep", fake_sleep)

    with pytest.raises(
        resume_mod.ResumeRetryableError, match="No remotion_webhook interrupt found"
    ):
        await resume_mod.resume_after_render(
            run_id, "https://example.com/out.mp4", raise_on_error=True
        )
    assert run_repo.updates == []
