from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import anyio
import pytest

from myloware.config import settings
from myloware.storage.models import JobStatus
from myloware.workers import worker as worker_mod


def test_default_worker_id_includes_host_and_pid(monkeypatch) -> None:
    monkeypatch.setattr(worker_mod.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(worker_mod.os, "getpid", lambda: 123)
    assert worker_mod._default_worker_id() == "host:123"


@pytest.mark.asyncio
async def test_lease_heartbeat_touches_lease_and_commits(monkeypatch) -> None:
    job_id = uuid4()
    calls: list[tuple[UUID, str, float]] = []
    commits = {"n": 0}
    stop_event = anyio.Event()

    class FakeSessionCM:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

    class FakeSessionFactory:
        def __call__(self):  # type: ignore[no-untyped-def]
            return FakeSessionCM()

    class FakeJobRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            return None

        async def touch_lease_async(self, job_id, *, worker_id, claim_generation, lease_seconds):  # type: ignore[no-untyped-def]
            calls.append((job_id, worker_id, float(lease_seconds)))
            stop_event.set()

    async def fake_commit(self):  # type: ignore[no-untyped-def]
        commits["n"] += 1

    # Patch session factory and repository
    monkeypatch.setattr(worker_mod, "get_async_session_factory", lambda: FakeSessionFactory())
    monkeypatch.setattr(worker_mod, "JobRepository", FakeJobRepo)
    monkeypatch.setattr(FakeSessionCM, "commit", fake_commit, raising=False)

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(worker_mod.anyio, "sleep", fake_sleep)

    await worker_mod._lease_heartbeat(job_id, "w1", 1, lease_seconds=9.0, stop_event=stop_event)

    assert calls and calls[0][0] == job_id


@pytest.mark.asyncio
async def test_lease_heartbeat_logs_warning_on_errors(monkeypatch) -> None:
    job_id = uuid4()
    stop_event = anyio.Event()

    class FakeSessionCM:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

        async def commit(self):  # type: ignore[no-untyped-def]
            return None

    class FakeSessionFactory:
        def __call__(self):  # type: ignore[no-untyped-def]
            return FakeSessionCM()

    class FakeJobRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            return None

        async def touch_lease_async(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            stop_event.set()
            raise RuntimeError("boom")

    monkeypatch.setattr(worker_mod, "get_async_session_factory", lambda: FakeSessionFactory())
    monkeypatch.setattr(worker_mod, "JobRepository", FakeJobRepo)

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(worker_mod.anyio, "sleep", fake_sleep)

    await worker_mod._lease_heartbeat(job_id, "w1", 1, lease_seconds=9.0, stop_event=stop_event)


@dataclass
class FakeJob:
    id: UUID
    job_type: str
    run_id: UUID | None
    payload: dict[str, object]
    attempts: int = 1
    max_attempts: int = 1
    claim_generation: int = 1


class FakeResult:
    def __init__(self, job: FakeJob | None) -> None:
        self._job = job

    def scalar_one_or_none(self) -> FakeJob | None:
        return self._job


class FakeSession:
    def __init__(self, job: FakeJob | None) -> None:
        self._job = job
        self.commits = 0

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        return None

    async def execute(self, _stmt):  # type: ignore[no-untyped-def]
        return FakeResult(self._job)

    async def commit(self) -> None:
        self.commits += 1


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def __call__(self):  # type: ignore[no-untyped-def]
        return self._session


class FakeJobRepo:
    def __init__(self, _session):  # type: ignore[no-untyped-def]
        self.succeeded: list[UUID] = []
        self.failed: list[UUID] = []
        self._status = JobStatus.FAILED
        self.last_failed_error: str | None = None
        self.last_failed_delay: float | None = None
        self.current_job: FakeJob | None = None

    async def get_current_claim_async(self, *_a, **_kw):  # type: ignore[no-untyped-def]
        return self.current_job

    async def mark_succeeded_async(self, job_id: UUID, **_kw) -> bool:  # type: ignore[no-untyped-def]
        self.succeeded.append(job_id)
        return True

    async def mark_failed_async(self, job_id: UUID, *, error: str, retry_delay_seconds: float, **_kw):  # type: ignore[no-untyped-def]
        self.failed.append(job_id)
        self.last_failed_error = str(error)
        self.last_failed_delay = float(retry_delay_seconds)
        return self._status

    async def touch_lease_async(self, *_a, **_kw):  # type: ignore[no-untyped-def]
        return None


class FakeRunRepo:
    def __init__(self, _session):  # type: ignore[no-untyped-def]
        return None


class FakeArtifactRepo:
    def __init__(self, _session):  # type: ignore[no-untyped-def]
        return None


class FakeDLQRepo:
    def __init__(self, _session):  # type: ignore[no-untyped-def]
        self.created: list[dict[str, object]] = []
        self.raise_on_create = False

    async def create_async(self, **kwargs):  # type: ignore[no-untyped-def]
        if self.raise_on_create:
            raise RuntimeError("dlq boom")
        self.created.append(kwargs)


@pytest.mark.asyncio
async def test_process_one_job_returns_when_job_missing(monkeypatch) -> None:
    session = FakeSession(job=None)
    monkeypatch.setattr(
        worker_mod, "get_async_session_factory", lambda: FakeSessionFactory(session)
    )
    monkeypatch.setattr(worker_mod, "JobRepository", FakeJobRepo)
    monkeypatch.setattr(worker_mod, "RunRepository", FakeRunRepo)
    monkeypatch.setattr(worker_mod, "ArtifactRepository", FakeArtifactRepo)
    monkeypatch.setattr(worker_mod, "DeadLetterRepository", FakeDLQRepo)

    await worker_mod._process_one_job(uuid4(), "w", 1, lease_seconds=1.0)
    assert session.commits == 0


@pytest.mark.asyncio
async def test_process_one_job_marks_succeeded(monkeypatch) -> None:
    job_id = uuid4()
    job = FakeJob(id=job_id, job_type="run.execute", run_id=None, payload={})
    session = FakeSession(job=job)

    repo = FakeJobRepo(object())
    repo.current_job = job
    dlq_repo = FakeDLQRepo(object())

    monkeypatch.setattr(
        worker_mod, "get_async_session_factory", lambda: FakeSessionFactory(session)
    )
    monkeypatch.setattr(worker_mod, "JobRepository", lambda _s: repo)
    monkeypatch.setattr(worker_mod, "RunRepository", FakeRunRepo)
    monkeypatch.setattr(worker_mod, "ArtifactRepository", FakeArtifactRepo)
    monkeypatch.setattr(worker_mod, "DeadLetterRepository", lambda _s: dlq_repo)
    monkeypatch.setattr(worker_mod, "get_sync_client", lambda: object())

    async def fake_handle_job(**_kw):  # type: ignore[no-untyped-def]
        return None

    async def fake_heartbeat(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(worker_mod, "handle_job", fake_handle_job)
    monkeypatch.setattr(worker_mod, "_lease_heartbeat", fake_heartbeat)

    await worker_mod._process_one_job(job_id, "w", 1, lease_seconds=1.0)

    assert repo.succeeded == [job_id]
    assert session.commits == 1
    assert dlq_repo.created == []


@pytest.mark.asyncio
async def test_process_one_job_reschedules_without_dlq(monkeypatch) -> None:
    from myloware.workers.exceptions import JobReschedule

    job_id = uuid4()
    run_id = uuid4()
    job = FakeJob(id=job_id, job_type="sora.poll", run_id=run_id, payload={"a": 1})
    session = FakeSession(job=job)

    repo = FakeJobRepo(object())
    repo.current_job = job
    repo._status = JobStatus.PENDING
    dlq_repo = FakeDLQRepo(object())

    monkeypatch.setattr(
        worker_mod, "get_async_session_factory", lambda: FakeSessionFactory(session)
    )
    monkeypatch.setattr(worker_mod, "JobRepository", lambda _s: repo)
    monkeypatch.setattr(worker_mod, "RunRepository", FakeRunRepo)
    monkeypatch.setattr(worker_mod, "ArtifactRepository", FakeArtifactRepo)
    monkeypatch.setattr(worker_mod, "DeadLetterRepository", lambda _s: dlq_repo)
    monkeypatch.setattr(worker_mod, "get_sync_client", lambda: object())
    monkeypatch.setattr(settings, "job_retry_delay_seconds", 0.0)

    async def fake_handle_job(**_kw):  # type: ignore[no-untyped-def]
        raise JobReschedule(retry_delay_seconds=12.5, reason="waiting")

    async def fake_heartbeat(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    async def fake_rollback(self):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(worker_mod, "handle_job", fake_handle_job)
    monkeypatch.setattr(worker_mod, "_lease_heartbeat", fake_heartbeat)
    monkeypatch.setattr(FakeSession, "rollback", fake_rollback, raising=False)

    await worker_mod._process_one_job(job_id, "w", 1, lease_seconds=1.0)

    assert repo.succeeded == []
    assert repo.failed == [job_id]
    assert repo.last_failed_error == "waiting"
    assert repo.last_failed_delay == 12.5
    assert dlq_repo.created == []
    assert session.commits == 1


@pytest.mark.asyncio
async def test_process_one_job_marks_failed_and_writes_dlq_on_terminal(monkeypatch) -> None:
    job_id = uuid4()
    run_id = uuid4()
    job = FakeJob(id=job_id, job_type="webhook.sora", run_id=run_id, payload={"a": 1})
    session = FakeSession(job=job)

    repo = FakeJobRepo(object())
    repo.current_job = job
    repo._status = JobStatus.FAILED
    dlq_repo = FakeDLQRepo(object())

    monkeypatch.setattr(
        worker_mod, "get_async_session_factory", lambda: FakeSessionFactory(session)
    )
    monkeypatch.setattr(worker_mod, "JobRepository", lambda _s: repo)
    monkeypatch.setattr(worker_mod, "RunRepository", FakeRunRepo)
    monkeypatch.setattr(worker_mod, "ArtifactRepository", FakeArtifactRepo)
    monkeypatch.setattr(worker_mod, "DeadLetterRepository", lambda _s: dlq_repo)
    monkeypatch.setattr(worker_mod, "get_sync_client", lambda: object())
    monkeypatch.setattr(settings, "job_retry_delay_seconds", 0.0)

    async def fake_handle_job(**_kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    async def fake_heartbeat(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(worker_mod, "handle_job", fake_handle_job)
    monkeypatch.setattr(worker_mod, "_lease_heartbeat", fake_heartbeat)

    await worker_mod._process_one_job(job_id, "w", 1, lease_seconds=1.0)

    assert repo.failed == [job_id]
    assert dlq_repo.created and dlq_repo.created[0]["source"] == "sora"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_process_one_job_swallow_dlq_write_failure(monkeypatch) -> None:
    job_id = uuid4()
    run_id = uuid4()
    job = FakeJob(id=job_id, job_type="webhook.sora", run_id=run_id, payload={"a": 1})
    session = FakeSession(job=job)

    repo = FakeJobRepo(object())
    repo.current_job = job
    repo._status = JobStatus.FAILED
    dlq_repo = FakeDLQRepo(object())
    dlq_repo.raise_on_create = True

    monkeypatch.setattr(
        worker_mod, "get_async_session_factory", lambda: FakeSessionFactory(session)
    )
    monkeypatch.setattr(worker_mod, "JobRepository", lambda _s: repo)
    monkeypatch.setattr(worker_mod, "RunRepository", FakeRunRepo)
    monkeypatch.setattr(worker_mod, "ArtifactRepository", FakeArtifactRepo)
    monkeypatch.setattr(worker_mod, "DeadLetterRepository", lambda _s: dlq_repo)
    monkeypatch.setattr(worker_mod, "get_sync_client", lambda: object())
    monkeypatch.setattr(settings, "job_retry_delay_seconds", 0.0)

    async def fake_handle_job(**_kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    async def fake_heartbeat(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(worker_mod, "handle_job", fake_handle_job)
    monkeypatch.setattr(worker_mod, "_lease_heartbeat", fake_heartbeat)

    await worker_mod._process_one_job(job_id, "w", 1, lease_seconds=1.0)

    assert repo.failed == [job_id]
    assert dlq_repo.created == []
    assert session.commits == 1


@pytest.mark.asyncio
async def test_run_worker_once_claim_none_exits(monkeypatch) -> None:
    monkeypatch.setattr(settings, "disable_background_workflows", False)
    monkeypatch.setattr(settings, "use_langgraph_engine", False)
    monkeypatch.setattr(settings, "worker_id", "w")
    monkeypatch.setattr(settings, "worker_concurrency", 1)
    monkeypatch.setattr(settings, "job_lease_seconds", 1.0)
    monkeypatch.setattr(settings, "job_poll_interval_seconds", 0.0)

    class FakeJobRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            return None

        async def claim_next_async(self, *, worker_id: str, lease_seconds: float):  # type: ignore[no-untyped-def]
            return None

    class FakeSessionCM:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return object()

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

        async def commit(self):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(worker_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM()))
    monkeypatch.setattr(worker_mod, "JobRepository", FakeJobRepo)

    await worker_mod.run_worker(once=True)


@pytest.mark.asyncio
async def test_run_worker_disallowed_when_background_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "disable_background_workflows", True)
    with pytest.raises(RuntimeError, match="Worker cannot run"):
        await worker_mod.run_worker(once=True)


@pytest.mark.asyncio
async def test_run_worker_once_processes_claimed_job(monkeypatch) -> None:
    monkeypatch.setattr(settings, "disable_background_workflows", False)
    monkeypatch.setattr(settings, "use_langgraph_engine", False)
    monkeypatch.setattr(settings, "worker_id", "w")
    monkeypatch.setattr(settings, "worker_concurrency", 1)
    monkeypatch.setattr(settings, "job_lease_seconds", 1.0)
    monkeypatch.setattr(settings, "job_poll_interval_seconds", 0.0)

    job_id = uuid4()

    class FakeJobRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            return None

        async def claim_next_async(self, *, worker_id: str, lease_seconds: float):  # type: ignore[no-untyped-def]
            assert worker_id == "w"
            return SimpleNamespace(id=job_id, claim_generation=1)

    class FakeSessionCM:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

        async def commit(self):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(worker_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM()))
    monkeypatch.setattr(worker_mod, "JobRepository", FakeJobRepo)
    process_one = AsyncMock(return_value=None)
    monkeypatch.setattr(worker_mod, "_process_one_job", process_one, raising=False)

    await worker_mod.run_worker(once=True)
    process_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_worker_initializes_langgraph_engine_for_non_sqlite(monkeypatch) -> None:
    monkeypatch.setattr(settings, "disable_background_workflows", False)
    monkeypatch.setattr(settings, "use_langgraph_engine", True)
    monkeypatch.setattr(settings, "database_url", "postgresql://example")
    monkeypatch.setattr(settings, "worker_id", "w")
    monkeypatch.setattr(settings, "worker_concurrency", 1)
    monkeypatch.setattr(settings, "job_lease_seconds", 1.0)
    monkeypatch.setattr(settings, "job_poll_interval_seconds", 0.0)

    engine_state: dict[str, object] = {"instance": None}
    ensure_called = AsyncMock()

    class FakeEngine:
        def __init__(self) -> None:
            engine_state["instance"] = self
            self.ensure_checkpointer_initialized = ensure_called

    set_engine = Mock()
    monkeypatch.setattr("myloware.workflows.langgraph.graph.LangGraphEngine", FakeEngine)
    monkeypatch.setattr("myloware.workflows.langgraph.graph.set_langgraph_engine", set_engine)

    class FakeJobRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            return None

        async def claim_next_async(self, *, worker_id: str, lease_seconds: float):  # type: ignore[no-untyped-def]
            return None

    class FakeSessionCM:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return object()

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

        async def commit(self):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(worker_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM()))
    monkeypatch.setattr(worker_mod, "JobRepository", FakeJobRepo)

    await worker_mod.run_worker(once=True)

    assert engine_state["instance"] is not None
    set_engine.assert_called_once()
    ensure_called.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_worker_loop_sleeps_when_no_jobs(monkeypatch) -> None:
    monkeypatch.setattr(settings, "disable_background_workflows", False)
    monkeypatch.setattr(settings, "use_langgraph_engine", False)
    monkeypatch.setattr(settings, "worker_id", "w")
    monkeypatch.setattr(settings, "worker_concurrency", 1)
    monkeypatch.setattr(settings, "job_lease_seconds", 1.0)
    monkeypatch.setattr(settings, "job_poll_interval_seconds", 0.01)

    class FakeJobRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            return None

        async def claim_next_async(self, *, worker_id: str, lease_seconds: float):  # type: ignore[no-untyped-def]
            return None

    class FakeSessionCM:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

        async def commit(self):  # type: ignore[no-untyped-def]
            return None

    class FakeLimiter:
        def __init__(self, _n: int):
            self.acquires = 0
            self.releases = 0

        async def acquire(self) -> None:
            self.acquires += 1
            if self.acquires > 1:
                raise RuntimeError("stop")

        def release(self) -> None:
            self.releases += 1

    class FakeTaskGroup:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def start_soon(self, _func, *_args):  # type: ignore[no-untyped-def]
            raise AssertionError("start_soon should not be called when no jobs are claimed")

    monkeypatch.setattr(worker_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM()))
    monkeypatch.setattr(worker_mod, "JobRepository", FakeJobRepo)
    monkeypatch.setattr(worker_mod.anyio, "sleep", lambda _s: asyncio.sleep(0))
    monkeypatch.setattr(worker_mod.anyio, "Semaphore", lambda n: FakeLimiter(n))
    monkeypatch.setattr(worker_mod.anyio, "create_task_group", lambda: FakeTaskGroup())
    housekeeping = AsyncMock(return_value=None)
    monkeypatch.setattr(worker_mod, "_run_studio_housekeeping", housekeeping)

    with pytest.raises(RuntimeError, match="stop"):
        await worker_mod.run_worker(once=False)
    housekeeping.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_worker_loop_starts_task_for_claimed_job(monkeypatch) -> None:
    monkeypatch.setattr(settings, "disable_background_workflows", False)
    monkeypatch.setattr(settings, "use_langgraph_engine", False)
    monkeypatch.setattr(settings, "worker_id", "w")
    monkeypatch.setattr(settings, "worker_concurrency", 1)
    monkeypatch.setattr(settings, "job_lease_seconds", 1.0)
    monkeypatch.setattr(settings, "job_poll_interval_seconds", 0.0)

    job_id = uuid4()

    class FakeJobRepo:
        def __init__(self, _session):  # type: ignore[no-untyped-def]
            return None

        async def claim_next_async(self, *, worker_id: str, lease_seconds: float):  # type: ignore[no-untyped-def]
            return SimpleNamespace(id=job_id, claim_generation=1)

    class FakeSessionCM:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

        async def commit(self):  # type: ignore[no-untyped-def]
            return None

    class FakeLimiter:
        def __init__(self, _n: int):
            self.acquires = 0
            self.releases = 0

        async def acquire(self) -> None:
            self.acquires += 1
            await asyncio.sleep(0)
            if self.acquires > 1:
                raise RuntimeError("stop")

        def release(self) -> None:
            self.releases += 1

    class FakeTaskGroup:
        def __init__(self) -> None:
            self._tasks: list[asyncio.Task[object]] = []

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            return False

        def start_soon(self, func, *args):  # type: ignore[no-untyped-def]
            self._tasks.append(asyncio.create_task(func(*args)))

    limiter = FakeLimiter(1)

    monkeypatch.setattr(worker_mod, "get_async_session_factory", lambda: (lambda: FakeSessionCM()))
    monkeypatch.setattr(worker_mod, "JobRepository", FakeJobRepo)
    monkeypatch.setattr(worker_mod.anyio, "Semaphore", lambda n: limiter)
    monkeypatch.setattr(worker_mod.anyio, "create_task_group", lambda: FakeTaskGroup())
    monkeypatch.setattr(worker_mod, "_process_one_job", AsyncMock(return_value=None), raising=False)

    with pytest.raises(RuntimeError, match="stop"):
        await worker_mod.run_worker(once=False)

    assert limiter.releases == 1
