from __future__ import annotations

import json
import hashlib
import hmac
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from myloware.config.settings import settings
from myloware.observability.logging import configure_logging
from myloware.storage.models import ArtifactType, RunStatus

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "webhooks"


def _load_webhook_fixture(name: str) -> bytes:
    return (_FIXTURES_DIR / name).read_bytes()


def _seed_clip_manifest(app: FastAPI, run_id: UUID, task_id: str) -> None:
    app.state._test_artifact_repo.create(
        run_id=run_id,
        persona="producer",
        artifact_type=ArtifactType.CLIP_MANIFEST,
        content=json.dumps({task_id: {"video_index": 0}}),
        metadata={"type": "task_metadata_mapping", "task_count": 1},
    )


class DummySession:
    def __init__(self) -> None:
        self.artifacts: list[SimpleNamespace] = []
        self.runs: dict[UUID, SimpleNamespace] = {}

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def flush(self) -> None: ...

    def close(self) -> None: ...


class DummyArtifact(SimpleNamespace):
    def __init__(
        self,
        run_id: UUID,
        persona: str,
        artifact_type: str,
        content: str | None = None,
        uri: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        super().__init__(
            run_id=run_id,
            persona=persona,
            artifact_type=artifact_type,
            content=content,
            uri=uri,
            artifact_metadata=metadata or {},
        )


class DummyArtifactRepository:
    def __init__(self, _session: DummySession) -> None:
        self.session = _session

    def create(
        self,
        run_id: UUID,
        persona: str,
        artifact_type: ArtifactType,
        content: str | None = None,
        uri: str | None = None,
        metadata: dict | None = None,
        trace_id: str | None = None,
    ) -> DummyArtifact:
        if not hasattr(self.session, "artifacts"):
            self.session.artifacts = []
        artifact = DummyArtifact(
            run_id=run_id,
            persona=persona,
            artifact_type=artifact_type.value,
            content=content,
            uri=uri,
            metadata=metadata,
        )
        self.session.artifacts.append(artifact)
        return artifact

    def get_by_run(self, run_id: UUID) -> list[DummyArtifact]:
        return list(self.session.artifacts)

    async def create_async(
        self,
        run_id: UUID,
        persona: str,
        artifact_type: ArtifactType,
        content: str | None = None,
        uri: str | None = None,
        metadata: dict | None = None,
        trace_id: str | None = None,
    ) -> DummyArtifact:
        return self.create(run_id, persona, artifact_type, content, uri, metadata, trace_id)

    async def get_by_run_async(self, run_id: UUID) -> list[DummyArtifact]:
        return self.get_by_run(run_id)


class DummyRun(SimpleNamespace):
    def __init__(self, run_id: UUID) -> None:
        super().__init__(
            id=run_id,
            status=RunStatus.AWAITING_VIDEO_GENERATION.value,
            error=None,
        )


class DummyRunRepository:
    def __init__(self, _session: DummySession) -> None:
        self.session = _session

    def get_for_update(self, run_id: UUID) -> DummyRun:
        if not hasattr(self.session, "runs"):
            self.session.runs = {}
        return self.session.runs.setdefault(run_id, DummyRun(run_id))

    async def get_for_update_async(self, run_id: UUID) -> DummyRun:
        return self.get_for_update(run_id)

    def get(self, run_id: UUID) -> DummyRun | None:
        if not hasattr(self.session, "runs"):
            self.session.runs = {}
        return self.session.runs.get(run_id)

    async def get_async(self, run_id: UUID) -> DummyRun | None:
        return self.get(run_id)

    def update(self, run_id: UUID, **kwargs) -> DummyRun:
        run = self.get_for_update(run_id)
        for key, value in kwargs.items():
            setattr(run, key, value)
        return run

    async def update_async(self, run_id: UUID, **kwargs) -> DummyRun:
        return self.update(run_id, **kwargs)

    async def add_artifact_async(self, *_args, **_kwargs):
        return None


def _make_hmac(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _build_app() -> FastAPI:
    # Minimal app importing only the webhooks router, not full workflows package.
    # Import is inside the function so it only happens when these tests run.
    from myloware.api.dependencies_async import get_async_artifact_repo, get_async_run_repo
    from myloware.api.routes.webhooks import router as webhooks_router  # type: ignore[import]

    app = FastAPI()
    app.include_router(webhooks_router)

    # Create shared session and repos for the entire app lifecycle
    session = DummySession()
    run_repo = DummyRunRepository(session)
    artifact_repo = DummyArtifactRepository(session)

    # Store in app state for test access
    app.state._test_session = session
    app.state._test_run_repo = run_repo
    app.state._test_artifact_repo = artifact_repo

    async def _override_run_repo():
        return run_repo

    async def _override_artifact_repo():
        return artifact_repo

    # Override deps to keep tests fully in-memory/no DB
    app.dependency_overrides[get_async_run_repo] = _override_run_repo
    app.dependency_overrides[get_async_artifact_repo] = _override_artifact_repo

    return app


@pytest.fixture(autouse=True)
def _configure_logging() -> None:
    """Ensure structlog is configured so caplog can capture events."""
    configure_logging()


@pytest.fixture(autouse=True)
def _stub_webhook_dependencies(monkeypatch) -> None:
    """Stub DB/session-heavy dependencies so tests stay in-memory.

    Note: FastAPI dependency overrides are set in _build_app() which handles
    get_async_run_repo and get_async_artifact_repo. This fixture patches
    module-level functions that bypass dependency injection.
    """
    from myloware.api.routes import webhooks  # type: ignore[import]

    session = DummySession()

    @contextmanager
    def fake_session() -> DummySession:
        yield session

    async def _noop_async(*args, **kwargs) -> None:
        return None

    # Patch sync session and async helpers that bypass FastAPI DI (only if present)
    if hasattr(webhooks, "get_session"):
        monkeypatch.setattr(webhooks, "get_session", fake_session)
    for attr in [
        "transcode_video",
        "_resume_langgraph_after_videos",
        "_resume_langgraph_after_render",
        "_update_run_after_render_async",
    ]:
        if hasattr(webhooks, attr):
            monkeypatch.setattr(webhooks, attr, _noop_async)

    # Patch Repository classes used in direct instantiation
    monkeypatch.setattr(webhooks, "ArtifactRepository", DummyArtifactRepository)
    monkeypatch.setattr(webhooks, "RunRepository", DummyRunRepository)


def test_valid_sora_signature_accepted(monkeypatch) -> None:
    app = _build_app()
    client = TestClient(app)

    secret = "test-secret"
    task_id = "video_test_123"
    payload = json.dumps(
        {
            "id": "evt_test_123",
            "object": "event",
            "created_at": 1_700_000_000,
            "type": "video.completed",
            "data": {"id": task_id},
        }
    ).encode("utf-8")

    # Sora standard webhook events don't include run_id, so we persist a mapping
    # from task_id -> metadata in CLIP_MANIFEST for validation.
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    app.state._test_artifact_repo.create(
        run_id=run_id,
        persona="producer",
        artifact_type=ArtifactType.CLIP_MANIFEST,
        content=json.dumps({task_id: {"video_index": 0}}),
        metadata={"type": "task_metadata_mapping", "task_count": 1},
    )

    monkeypatch.setattr(settings, "openai_sora_signing_secret", "")
    monkeypatch.setattr(settings, "openai_standard_webhook_secret", secret)
    monkeypatch.setattr(settings, "sora_provider", "real")
    monkeypatch.setattr(settings, "disable_background_workflows", False)
    monkeypatch.setattr(settings, "workflow_dispatcher", "inprocess")

    from myloware.api.routes import webhooks  # type: ignore[import]

    async def _fake_download(_video_id: str):  # type: ignore[no-untyped-def]
        import tempfile

        path = Path(tempfile.gettempdir()) / "openai_video_test.mp4"
        path.write_bytes(b"fake")
        return path

    monkeypatch.setattr(webhooks, "download_openai_video_content_to_tempfile", _fake_download)

    import openai

    class _FakeWebhooks:
        def verify_signature(self, *, payload: bytes, headers, secret: str) -> None:  # type: ignore[no-untyped-def]
            assert secret == "test-secret"
            assert headers.get("webhook-id") == "evt_test_123"
            assert headers.get("webhook-timestamp") == "1700000000"
            assert headers.get("webhook-signature") == "v1,valid"
            assert payload.startswith(b"{")

    class _FakeOpenAI:
        def __init__(self, api_key: str):  # noqa: ARG002
            self.webhooks = _FakeWebhooks()

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    response = client.post(
        f"/v1/webhooks/sora?run_id={run_id}",
        content=payload,
        headers={
            "webhook-id": "evt_test_123",
            "webhook-timestamp": "1700000000",
            "webhook-signature": "v1,valid",
        },
    )

    assert response.status_code == 200


def test_sora_webhook_contract_completed(monkeypatch) -> None:
    app = _build_app()
    client = TestClient(app)

    run_id = UUID("00000000-0000-0000-0000-000000000001")
    task_id = "video_abc123"
    _seed_clip_manifest(app, run_id, task_id)

    from myloware.api.routes import webhooks  # type: ignore[import]

    async def _fake_transcode(*_args, **_kwargs) -> str:
        return "https://example.com/transcoded.mp4"

    monkeypatch.setattr(webhooks, "transcode_video", _fake_transcode)
    monkeypatch.setattr(settings, "sora_provider", "fake")
    monkeypatch.setattr(settings, "disable_background_workflows", False)
    monkeypatch.setattr(settings, "workflow_dispatcher", "inprocess")
    monkeypatch.setattr(settings, "webhook_base_url", "https://example.com")

    payload = _load_webhook_fixture("sora_video_completed.json")
    response = client.post(
        f"/v1/webhooks/sora?run_id={run_id}",
        content=payload,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["video_url"] == "https://example.com/transcoded.mp4"


def test_sora_webhook_contract_failed(monkeypatch) -> None:
    app = _build_app()
    client = TestClient(app)

    run_id = UUID("00000000-0000-0000-0000-000000000001")
    task_id = "video_abc123"
    _seed_clip_manifest(app, run_id, task_id)

    monkeypatch.setattr(settings, "sora_provider", "fake")
    monkeypatch.setattr(settings, "disable_background_workflows", False)
    monkeypatch.setattr(settings, "workflow_dispatcher", "inprocess")

    payload = _load_webhook_fixture("sora_video_failed.json")
    response = client.post(
        f"/v1/webhooks/sora?run_id={run_id}",
        content=payload,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["error"] == "video.failed"


def test_sora_webhook_downloads_openai_content_when_missing_urls(monkeypatch, tmp_path) -> None:
    app = _build_app()
    client = TestClient(app)

    run_id = UUID("00000000-0000-0000-0000-000000000001")
    task_id = "video_abc123"
    _seed_clip_manifest(app, run_id, task_id)

    from myloware.api.routes import webhooks  # type: ignore[import]

    async def _fake_download(_video_id: str):
        path = tmp_path / "openai_video.mp4"
        path.write_bytes(b"fake")
        return path

    async def _fake_transcode(*_args, **_kwargs) -> str:
        return "https://example.com/transcoded.mp4"

    monkeypatch.setattr(webhooks, "download_openai_video_content_to_tempfile", _fake_download)
    monkeypatch.setattr(webhooks, "transcode_video", _fake_transcode)

    monkeypatch.setattr(settings, "sora_provider", "real")
    monkeypatch.setattr(settings, "disable_background_workflows", False)
    monkeypatch.setattr(settings, "workflow_dispatcher", "inprocess")
    monkeypatch.setattr(settings, "openai_standard_webhook_secret", "test-secret")
    monkeypatch.setattr(settings, "openai_sora_signing_secret", "")

    import openai

    class _FakeWebhooks:
        def verify_signature(self, *, payload: bytes, headers, secret: str) -> None:  # type: ignore[no-untyped-def]
            return None

    class _FakeOpenAI:
        def __init__(self, api_key: str):  # noqa: ARG002
            self.webhooks = _FakeWebhooks()

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    payload = _load_webhook_fixture("sora_video_completed.json")
    response = client.post(
        f"/v1/webhooks/sora?run_id={run_id}",
        content=payload,
        headers={
            "webhook-id": "evt_test_123",
            "webhook-timestamp": "1700000000",
            "webhook-signature": "v1,valid",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["video_url"] == "https://example.com/transcoded.mp4"


def test_invalid_sora_signature_rejected(monkeypatch, caplog) -> None:
    app = _build_app()
    client = TestClient(app)

    payload = json.dumps(
        {
            "id": "evt_test_123",
            "object": "event",
            "created_at": 1_700_000_000,
            "type": "video.completed",
            "data": {"id": "video_test_123"},
        }
    ).encode("utf-8")
    secret = "test-secret"

    monkeypatch.setattr(settings, "openai_sora_signing_secret", "")
    monkeypatch.setattr(settings, "openai_standard_webhook_secret", secret)
    monkeypatch.setattr(settings, "sora_provider", "real")
    monkeypatch.setattr(settings, "disable_background_workflows", False)

    import openai

    class _FakeWebhooks:
        def verify_signature(self, *, payload: bytes, headers, secret: str) -> None:  # type: ignore[no-untyped-def]
            raise ValueError("bad signature")

    class _FakeOpenAI:
        def __init__(self, api_key: str):  # noqa: ARG002
            self.webhooks = _FakeWebhooks()

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    caplog.set_level("WARNING")

    response = client.post(
        "/v1/webhooks/sora?run_id=00000000-0000-0000-0000-000000000001",
        content=payload,
        headers={
            "webhook-id": "evt_test_123",
            "webhook-timestamp": "1700000000",
            "webhook-signature": "v1,invalid",
        },
    )

    assert response.status_code == 401

    assert any(
        "signature verification failed" in record.message.lower() for record in caplog.records
    )


def test_remotion_missing_secret_fails_in_prod(monkeypatch) -> None:
    app = _build_app()
    client = TestClient(app)

    payload = b'{"status": "done", "output_url": "https://example.com/v.mp4"}'
    run_id = UUID("00000000-0000-0000-0000-000000000001")

    # Create a run in the app's shared repository (from _build_app)
    run_repo = app.state._test_run_repo
    run = run_repo.get_for_update(run_id)  # Creates the run
    run.status = RunStatus.AWAITING_RENDER.value

    # No secret configured, real mode should fail
    monkeypatch.setattr(settings, "remotion_provider", "real")
    monkeypatch.setattr(settings, "disable_background_workflows", False)
    monkeypatch.setattr(settings, "remotion_webhook_secret", "")

    response = client.post(
        f"/v1/webhooks/remotion?run_id={run_id}",
        content=payload,
        headers={"X-Remotion-Signature": "anything"},
    )

    assert response.status_code == 500


def test_remotion_missing_secret_allows_in_fake_mode(monkeypatch) -> None:
    app = _build_app()
    client = TestClient(app)

    payload = b'{"status": "done", "output_url": "https://example.com/v.mp4"}'
    run_id = UUID("00000000-0000-0000-0000-000000000001")

    run_repo = app.state._test_run_repo
    run = run_repo.get_for_update(run_id)
    run.status = RunStatus.AWAITING_RENDER.value

    monkeypatch.setattr(settings, "remotion_provider", "fake")
    monkeypatch.setattr(settings, "disable_background_workflows", True)
    monkeypatch.setattr(settings, "remotion_webhook_secret", "")

    response = client.post(
        f"/v1/webhooks/remotion?run_id={run_id}",
        content=payload,
        headers={"X-Remotion-Signature": "anything"},
    )

    assert response.status_code == 200


def test_sora_missing_signature_fails_in_prod(monkeypatch) -> None:
    """Test that missing signature header fails in production when secret is configured."""
    app = _build_app()
    client = TestClient(app)

    payload = b'{"id":"evt_test_123","object":"event","created_at":1700000000,"type":"video.completed","data":{"id":"video_test_123"}}'

    monkeypatch.setattr(settings, "sora_provider", "real")
    monkeypatch.setattr(settings, "disable_background_workflows", False)
    monkeypatch.setattr(settings, "openai_sora_signing_secret", "")
    monkeypatch.setattr(settings, "openai_standard_webhook_secret", "test-secret")

    response = client.post(
        "/v1/webhooks/sora?run_id=00000000-0000-0000-0000-000000000001",
        content=payload,
        # No signature header
    )

    assert response.status_code == 401


def test_sora_missing_secret_allows_in_fake_mode(monkeypatch) -> None:
    """Test that missing secret allows webhook in fake mode."""
    app = _build_app()
    client = TestClient(app)

    payload = b'{"code": 200, "data": {"info": {"resultUrls": ["https://example.com/v.mp4"]}}}'

    monkeypatch.setattr(settings, "sora_provider", "fake")
    monkeypatch.setattr(settings, "disable_background_workflows", True)
    monkeypatch.setattr(settings, "openai_sora_signing_secret", "")

    response = client.post(
        "/v1/webhooks/sora?run_id=00000000-0000-0000-0000-000000000001",
        content=payload,
        headers={"X-Sora-Signature": "anything"},
    )

    assert response.status_code == 200


def test_signature_verification_uses_constant_time(monkeypatch) -> None:
    from myloware.api.routes import webhooks  # type: ignore

    payload = b"{}"
    secret = "constant-time"
    expected_signature = _make_hmac(secret, payload)
    calls: list[tuple[str, str]] = []

    def _recording_compare_digest(a: str, b: str) -> bool:
        calls.append((a, b))
        return True

    monkeypatch.setattr(webhooks.hmac, "compare_digest", _recording_compare_digest)

    result = webhooks._verify_webhook_signature(
        payload,
        signature=expected_signature,
        secret=secret,
        source="sora",
    )

    assert result is True
    assert calls == [(expected_signature, expected_signature)]
