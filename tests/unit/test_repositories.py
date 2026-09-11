"""Unit tests for run repository."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from myloware.storage.models import ArtifactType, Base, Job, JobStatus, RunStatus
from myloware.storage.repositories import (
    ArtifactRepository,
    AuditLogRepository,
    ChatSessionRepository,
    DeadLetterRepository,
    FeedbackRepository,
    JobRepository,
    RunRepository,
    StaleJobClaim,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def run_repo(db_session):
    return RunRepository(db_session)


@pytest.fixture
def artifact_repo(db_session):
    return ArtifactRepository(db_session)


def test_create_run(run_repo):
    run = run_repo.create(workflow_name="aismr", input="Test brief")

    assert run.id is not None
    assert run.workflow_name == "aismr"
    assert run.input == "Test brief"
    assert run.status == RunStatus.PENDING.value


def test_get_run(run_repo):
    created = run_repo.create("aismr", "Test")

    found = run_repo.get(created.id)

    assert found is not None
    assert found.id == created.id


def test_get_run_not_found(run_repo):
    found = run_repo.get(uuid.uuid4())
    assert found is None


def test_update_status(run_repo):
    run = run_repo.create("aismr", "Test")

    updated = run_repo.update_status(run.id, RunStatus.RUNNING)

    assert updated.status == RunStatus.RUNNING.value


def test_add_artifact(run_repo):
    run = run_repo.create("aismr", "Test")

    run_repo.add_artifact(run.id, "ideas", "Some ideas here")

    updated = run_repo.get(run.id)
    assert updated.artifacts["ideas"] == "Some ideas here"


def test_list_runs(run_repo):
    run_repo.create("aismr", "Test 1")
    run_repo.create("aismr", "Test 2")
    run_repo.create("aismr", "Test 3")

    runs = run_repo.list(limit=2)

    assert len(runs) == 2


def test_list_runs_by_status(run_repo):
    run_repo.create("aismr", "Test 1")
    run2 = run_repo.create("aismr", "Test 2")
    run_repo.update_status(run2.id, RunStatus.COMPLETED)

    pending = run_repo.list(status=RunStatus.PENDING)
    completed = run_repo.list(status=RunStatus.COMPLETED)

    assert len(pending) == 1
    assert len(completed) == 1


def test_update_step(run_repo):
    run = run_repo.create("aismr", "Test")
    run_repo.update_step(run.id, "ideation")

    updated = run_repo.get(run.id)
    assert updated.current_step == "ideation"


def test_create_artifact(run_repo, artifact_repo):
    run = run_repo.create("aismr", "Test brief")
    artifact = artifact_repo.create(
        run_id=run.id,
        persona="ideator",
        artifact_type=ArtifactType.IDEAS,
        content="idea text",
        metadata={"step": "ideator"},
        trace_id="trace-123",
    )

    assert artifact.id is not None
    assert artifact.artifact_metadata.get("trace_id") == "trace-123"
    assert artifact.artifact_type == ArtifactType.IDEAS.value


def test_get_artifacts_by_run(run_repo, artifact_repo):
    run = run_repo.create("aismr", "Test brief")
    artifact_repo.create(run.id, "ideator", ArtifactType.IDEAS, content="ideas")
    artifact_repo.create(run.id, "producer", ArtifactType.CLIP_MANIFEST, content="clips")

    artifacts = artifact_repo.get_by_run(run.id)

    assert len(artifacts) == 2
    assert artifacts[0].persona == "ideator"
    assert artifacts[1].artifact_type == ArtifactType.CLIP_MANIFEST.value


def test_get_artifact_by_type(run_repo, artifact_repo):
    run = run_repo.create("aismr", "Test brief")
    artifact_repo.create(run.id, "ideator", ArtifactType.IDEAS, content="ideas")

    found = artifact_repo.get_by_type(run.id, ArtifactType.IDEAS)

    assert found is not None
    assert found.content == "ideas"


@pytest.mark.asyncio
async def test_create_async_raises_with_sync_session(run_repo):
    with pytest.raises(TypeError, match="Use create\\(\\) with Session"):
        # type: ignore[arg-type]
        await run_repo.create_async("aismr", "Test")


def test_normalize_status_accepts_uppercase():
    assert RunRepository._normalize_status("FAILED") == RunStatus.FAILED.value


def test_normalize_status_rejects_invalid():
    with pytest.raises(ValueError):
        RunRepository._normalize_status("not-a-status")


def test_get_latest_artifact_by_type(run_repo, artifact_repo):
    run = run_repo.create("aismr", "Test brief")
    artifact_repo.create(run.id, "ideator", ArtifactType.IDEAS, content="older")
    latest = artifact_repo.create(run.id, "ideator", ArtifactType.IDEAS, content="newer")

    found = artifact_repo.get_latest_artifact_by_type(run.id, ArtifactType.IDEAS)
    assert found is not None
    assert found.id == latest.id


def test_find_cached_videos_by_topic_with_signs(run_repo, artifact_repo):
    run = run_repo.create("aismr", "Test")
    artifact_repo.create(
        run.id,
        "producer",
        ArtifactType.VIDEO_CLIP,
        uri="https://example.com/a.mp4",
        metadata={"topic": "zodiac", "sign": "Aries"},
    )
    artifact_repo.create(
        run.id,
        "producer",
        ArtifactType.VIDEO_CLIP,
        uri="https://example.com/b.mp4",
        metadata={"topic": "zodiac", "sign": "Taurus"},
    )

    results = artifact_repo.find_cached_videos("zodiac", signs=["Taurus", "Aries"], limit=2)
    assert [r.uri for r in results] == ["https://example.com/b.mp4", "https://example.com/a.mp4"]


def test_count_cached_videos_by_topic(run_repo, artifact_repo):
    run = run_repo.create("aismr", "Test")
    artifact_repo.create(
        run.id,
        "producer",
        ArtifactType.VIDEO_CLIP,
        uri="https://example.com/a.mp4",
        metadata={"topic": "rain"},
    )
    artifact_repo.create(
        run.id,
        "producer",
        ArtifactType.VIDEO_CLIP,
        uri="https://example.com/b.mp4",
        metadata={"topic": "rain"},
    )
    assert artifact_repo.count_cached_videos_by_topic("rain") == 2


def test_chat_session_repository_round_trip(db_session):
    repo = ChatSessionRepository(db_session)
    created = repo.create_or_update_session("user-1", "session-1")
    assert created.session_id == "session-1"
    assert repo.get_session("user-1") == "session-1"


@pytest.mark.asyncio
async def test_find_run_for_sora_task_async(monkeypatch):
    from myloware.storage import repositories

    class FakeArtifact:
        def __init__(self, run_id, content, metadata):
            self.run_id = run_id
            self.content = content
            self.artifact_metadata = metadata

    class FakeResult:
        def __init__(self, artifacts):
            self._artifacts = artifacts

        def scalars(self):
            return self

        def all(self):
            return self._artifacts

    class FakeAsyncSession:
        async def execute(self, _query):  # type: ignore[no-untyped-def]
            return FakeResult(artifacts)

    monkeypatch.setattr(repositories, "AsyncSession", FakeAsyncSession)

    run_id = uuid.uuid4()
    artifacts = [
        FakeArtifact(
            run_id=run_id,
            content='{"task_123": {"video_index": 0}}',
            metadata={"type": "task_metadata_mapping"},
        )
    ]
    repo = ArtifactRepository(FakeAsyncSession())
    out = await repo.find_run_for_sora_task_async("task_123")
    assert out is not None
    found_run_id, meta = out
    assert str(found_run_id) == str(run_id)
    assert meta["video_index"] == 0


@pytest.fixture
async def async_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


def test_find_by_status_and_age(run_repo):
    run = run_repo.create("aismr", "Test")
    run_repo.update_status(run.id, RunStatus.COMPLETED)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=1)
    results = run_repo.find_by_status_and_age(RunStatus.COMPLETED.value, cutoff)
    assert run in results


def test_chat_session_repository_delete(db_session):
    repo = ChatSessionRepository(db_session)
    repo.create_or_update_session("user-2", "session-2")
    assert repo.delete_session("user-2") is True
    assert repo.get_session("user-2") is None


def test_audit_log_repository_create_and_get(db_session):
    repo = AuditLogRepository(db_session)
    run_id = uuid.uuid4()
    repo.create(action="start", user_id="u1", run_id=run_id, outcome="ok")
    assert repo.get_by_run_id(run_id)
    assert repo.get_by_user_id("u1")


def test_feedback_repository_positive_feedback(db_session, run_repo):
    run = run_repo.create("aismr", "Test")
    repo = FeedbackRepository(db_session)
    repo.create(run.id, rating=5, comment="great")
    repo.create(run.id, rating=2, comment="bad")
    positive = repo.get_positive_feedback(limit=10)
    assert len(positive) == 1
    assert positive[0].rating == 5


def test_dead_letter_repository_sync(db_session, run_repo):
    run = run_repo.create("aismr", "Test")
    repo = DeadLetterRepository(db_session)
    dead = repo.create(source="sora", run_id=run.id, payload={"x": 1})
    assert repo.get(dead.id) is not None
    assert repo.get_unresolved()
    repo.increment_attempts(dead.id)
    repo.mark_resolved(dead.id)
    assert repo.get_unresolved() == []


@pytest.mark.asyncio
async def test_run_repo_async_helpers(async_session):
    run_repo = RunRepository(async_session)
    run = await run_repo.create_async("aismr", "brief", telegram_chat_id="123")
    await async_session.commit()

    fetched = await run_repo.get_async(run.id)
    assert fetched is not None
    locked = await run_repo.get_for_update_async(run.id)
    assert locked is not None

    await run_repo.add_artifact_async(run.id, "ideas", {"a": 1})
    await async_session.commit()
    updated = await run_repo.get_async(run.id)
    assert updated.artifacts["ideas"]["a"] == 1

    await run_repo.update_async(run.id, status=RunStatus.COMPLETED.value, current_step="done")
    await async_session.commit()
    updated = await run_repo.get_async(run.id)
    assert updated.status == RunStatus.COMPLETED.value
    assert updated.current_step == "done"

    count = await run_repo.count_runs_since_async(datetime.now(timezone.utc) - timedelta(hours=1))
    assert count >= 1


@pytest.mark.asyncio
async def test_feedback_repository_async(async_session):
    run_repo = RunRepository(async_session)
    run = await run_repo.create_async("aismr", "Test")
    await async_session.commit()
    repo = FeedbackRepository(async_session)
    await repo.create_async(run.id, rating=4, comment="ok")
    await async_session.commit()
    results = await repo.get_by_run_id_async(run.id)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_dead_letter_repository_async(async_session):
    run_repo = RunRepository(async_session)
    run = await run_repo.create_async("aismr", "Test")
    await async_session.commit()
    repo = DeadLetterRepository(async_session)
    dead = await repo.create_async(source="remotion", run_id=run.id, payload={"y": 2})
    await async_session.commit()
    assert await repo.get_async(dead.id) is not None
    assert await repo.get_unresolved_async()
    await repo.increment_attempts_async(dead.id)
    await repo.mark_resolved_async(dead.id)
    await async_session.commit()
    assert await repo.get_unresolved_async() == []


@pytest.mark.asyncio
async def test_job_repository_lifecycle(async_session):
    repo = JobRepository(async_session)
    run_id = uuid.uuid4()
    await repo.enqueue_async(
        "webhook",
        run_id=run_id,
        payload={"ok": True},
        idempotency_key="idem-1",
        max_attempts=2,
    )
    await async_session.commit()

    claimed = await repo.claim_next_async(worker_id="worker-1", lease_seconds=5)
    assert claimed is not None
    assert claimed.status == JobStatus.RUNNING.value
    assert claimed.claim_generation == 1

    assert await repo.touch_lease_async(
        claimed.id, worker_id="worker-1", claim_generation=claimed.claim_generation, lease_seconds=5
    )
    await async_session.commit()

    assert await repo.mark_succeeded_async(
        claimed.id, worker_id="worker-1", claim_generation=claimed.claim_generation
    )
    await async_session.commit()
    refreshed = await async_session.get(Job, claimed.id)
    assert refreshed.status == JobStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_job_repository_failed_and_reschedule(async_session):
    repo = JobRepository(async_session)
    job = await repo.enqueue_async(
        "webhook",
        payload={"ok": False},
        idempotency_key="idem-2",
        max_attempts=2,
    )
    await async_session.commit()

    claimed = await repo.claim_next_async(worker_id="worker-1")
    assert claimed is not None
    status = await repo.mark_failed_async(
        claimed.id,
        worker_id="worker-1",
        claim_generation=claimed.claim_generation,
        error="boom",
        retry_delay_seconds=1,
    )
    await async_session.commit()
    assert status == JobStatus.PENDING

    await async_session.refresh(job)
    job.available_at = repo._utc_now_naive()
    await async_session.commit()
    claimed = await repo.claim_next_async(worker_id="worker-2")
    assert claimed is not None
    status = await repo.mark_failed_async(
        claimed.id,
        worker_id="worker-2",
        claim_generation=claimed.claim_generation,
        error="boom-again",
    )
    await async_session.commit()
    assert status == JobStatus.FAILED


@pytest.mark.asyncio
async def test_job_claim_generation_fences_stale_same_worker_id(tmp_path) -> None:
    """A reclaimed lease cannot be mutated by an older process using the same ID."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'claims.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        repo = JobRepository(session)
        job = await repo.enqueue_async("fenced", idempotency_key="fenced-1", max_attempts=3)
        await session.commit()
        first = await repo.claim_next_async(worker_id="same-worker", lease_seconds=60)
        assert first is not None
        assert first.claim_generation == 1
        job_id = job.id
        await session.commit()

    async with Session() as session:
        repo = JobRepository(session)
        current = await session.get(Job, job_id)
        assert current is not None
        current.lease_expires_at = repo._utc_now_naive() - timedelta(seconds=1)
        await session.commit()
        second = await repo.claim_next_async(worker_id="same-worker", lease_seconds=60)
        assert second is not None
        assert second.claim_generation == 2
        await session.commit()

    async with Session() as session:
        repo = JobRepository(session)
        with pytest.raises(StaleJobClaim):
            await repo.touch_lease_async(
                job_id, worker_id="same-worker", claim_generation=first.claim_generation
            )
        with pytest.raises(StaleJobClaim):
            await repo.mark_succeeded_async(
                job_id, worker_id="same-worker", claim_generation=first.claim_generation
            )
        with pytest.raises(StaleJobClaim):
            await repo.mark_failed_async(
                job_id,
                worker_id="same-worker",
                claim_generation=first.claim_generation,
                error="old process failed",
            )
        await session.rollback()

    async with Session() as session:
        current = await session.get(Job, job_id)
        assert current is not None
        assert current.status == JobStatus.RUNNING.value
        assert current.claimed_by == "same-worker"
        assert current.claim_generation == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_job_repository_idempotency(async_session):
    repo = JobRepository(async_session)
    await repo.enqueue_async("webhook", idempotency_key="idem-dup")
    await async_session.commit()
    with pytest.raises(ValueError, match="job_already_enqueued"):
        await repo.enqueue_async("webhook", idempotency_key="idem-dup")


def test_create_run_invalid_telegram_chat_id(run_repo):
    run = run_repo.create("aismr", "Test", telegram_chat_id="not-int")
    assert run.telegram_chat_id is None


def test_get_for_update(run_repo):
    run = run_repo.create("aismr", "Test")
    locked = run_repo.get_for_update(run.id)
    assert locked is not None


def test_get_by_id_str(run_repo):
    run = run_repo.create("aismr", "Test")
    found = run_repo.get_by_id_str(str(run.id))
    assert found is not None


def test_add_artifact_returns_none_for_missing_run(run_repo):
    assert run_repo.add_artifact(uuid.uuid4(), "ideas", "x") is None


def test_list_runs_by_user_id(run_repo):
    run_repo.create("aismr", "Test 1", user_id="u1")
    run_repo.create("aismr", "Test 2", user_id="u2")
    runs = run_repo.list(user_id="u1")
    assert len(runs) == 1


def test_normalize_status_invalid_raises(run_repo):
    with pytest.raises(ValueError):
        run_repo._normalize_status("not-a-status")


def test_find_cached_videos_signs_and_limit(artifact_repo, run_repo):
    run = run_repo.create("aismr", "Test")
    artifact_repo.create(
        run_id=run.id,
        persona="producer",
        artifact_type=ArtifactType.VIDEO_CLIP,
        uri="https://cdn/1",
        metadata={"topic": "cats", "sign": "aries"},
    )
    artifact_repo.create(
        run_id=run.id,
        persona="producer",
        artifact_type=ArtifactType.VIDEO_CLIP,
        uri="https://cdn/2",
        metadata={"topic": "cats", "sign": "taurus"},
    )

    results = artifact_repo.find_cached_videos("cats", signs=["taurus", "aries"], limit=1)
    assert len(results) == 1
    assert results[0].artifact_metadata.get("sign") == "taurus"

    results_all = artifact_repo.find_cached_videos("cats")
    assert len(results_all) >= 2


@pytest.mark.asyncio
async def test_find_run_for_sora_task_async_requires_async_session(db_session):
    repo = ArtifactRepository(db_session)
    with pytest.raises(TypeError):
        await repo.find_run_for_sora_task_async("task")


@pytest.mark.asyncio
async def test_find_run_for_sora_task_async_empty_task(async_session):
    repo = ArtifactRepository(async_session)
    assert await repo.find_run_for_sora_task_async("") is None


def test_dead_letter_repository_get_unresolved_filters_source(db_session, run_repo):
    repo = DeadLetterRepository(db_session)
    run = run_repo.create("aismr", "Test")
    repo.create(source="sora", run_id=run.id, payload={"x": 1})
    repo.create(source="remotion", run_id=run.id, payload={"x": 2})
    unresolved = repo.get_unresolved(source="sora")
    assert all(dl.source == "sora" for dl in unresolved)


def test_job_repository_dialect_name_unknown():
    class FakeSession:
        def get_bind(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    repo = JobRepository(FakeSession())  # type: ignore[arg-type]
    assert repo._dialect_name() == "unknown"


@pytest.mark.asyncio
async def test_job_repository_type_errors(db_session):
    repo = JobRepository(db_session)
    with pytest.raises(TypeError):
        await repo.enqueue_async("job")
    with pytest.raises(TypeError):
        await repo.claim_next_async(worker_id="w")
    with pytest.raises(TypeError):
        await repo.touch_lease_async(uuid.uuid4(), worker_id="w", claim_generation=1)
    with pytest.raises(TypeError):
        await repo.get_current_claim_async(uuid.uuid4(), worker_id="w", claim_generation=1)
    with pytest.raises(TypeError):
        await repo.mark_succeeded_async(uuid.uuid4(), worker_id="w", claim_generation=1)
    with pytest.raises(TypeError):
        await repo.mark_failed_async(uuid.uuid4(), worker_id="w", claim_generation=1, error="err")
