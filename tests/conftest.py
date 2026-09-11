"""Pytest configuration and shared fixtures."""

import os
import sys
import tempfile
from pathlib import Path

import pytest
import httpx

# Ensure source tree is importable without editable install (Python >=3.12 not always available)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


def pytest_configure(config):
    """Configure pytest markers and environment for tests."""
    config.addinivalue_line("markers", "integration: marks tests as integration tests")

    # Set safe defaults for unit tests to avoid external dependencies
    # These MUST override any developer shell/.env values to keep the test run deterministic.
    os.environ["ENVIRONMENT"] = "test"
    os.environ["USE_FAKE_PROVIDERS"] = "false"
    os.environ["LLAMA_STACK_PROVIDER"] = "fake"
    os.environ["SORA_PROVIDER"] = "fake"
    os.environ["REMOTION_PROVIDER"] = "fake"
    os.environ["UPLOAD_POST_PROVIDER"] = "fake"
    os.environ["DISABLE_BACKGROUND_WORKFLOWS"] = "true"
    os.environ["FAIL_FAST_ON_STARTUP"] = "false"
    os.environ["WEBHOOK_BASE_URL"] = "http://localhost:8000"
    # Enable websearch tool config without making any external calls in tests (LLAMA_STACK_PROVIDER=fake).
    os.environ["BRAVE_API_KEY"] = os.environ.get("BRAVE_API_KEY") or "test-brave-key"
    # Keep ordinary tests on SQLite. The dedicated parity invocation can opt in
    # to an explicitly selected disposable PostgreSQL target.
    #
    # IMPORTANT: do not write SQLite DB files into the repo root (they can become stale and
    # cause confusing schema mismatch failures). Use a per-test-run temp directory instead.
    parity_requested = "parity" in (config.getoption("markexpr") or "") or any(
        "test_studio_postgres_parity.py" in str(arg) for arg in config.args
    )
    parity_url = os.environ.get("AISMR_TEST_POSTGRES_URL")
    if parity_requested and parity_url:
        os.environ["DATABASE_URL"] = parity_url
    else:
        test_artifacts_root = Path(
            os.environ.get("MYLOWARE_TEST_ARTIFACTS_DIR") or (PROJECT_ROOT / ".tmp" / "pytest")
        )
        test_artifacts_root.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="run_", dir=str(test_artifacts_root)))
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{run_dir / 'test_default.db'}"


@pytest.fixture(autouse=True)
def block_external_http(monkeypatch):
    """Fail the fast lane if code tries to hit the public internet.

    Allowlist only:
    - ASGI test host ("test") used with httpx.ASGITransport
    - localhost/loopback for local services
    - requests whose selected HTTP transport is an in-memory MockTransport
    """

    allowed_hosts = {"test", "testserver", "localhost", "127.0.0.1", "0.0.0.0"}

    async def _async_guard(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        u = httpx.URL(url) if not isinstance(url, httpx.URL) else url
        mocked = isinstance(self._transport_for_url(u), httpx.MockTransport)
        if not mocked and u.scheme in {"http", "https"} and (u.host or "") not in allowed_hosts:
            raise RuntimeError(f"External HTTP blocked in tests: {u!s}")
        return await _orig_async_request(self, method, url, *args, **kwargs)

    def _sync_guard(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        u = httpx.URL(url) if not isinstance(url, httpx.URL) else url
        mocked = isinstance(self._transport_for_url(u), httpx.MockTransport)
        if not mocked and u.scheme in {"http", "https"} and (u.host or "") not in allowed_hosts:
            raise RuntimeError(f"External HTTP blocked in tests: {u!s}")
        return _orig_sync_request(self, method, url, *args, **kwargs)

    _orig_async_request = httpx.AsyncClient.request
    _orig_sync_request = httpx.Client.request
    monkeypatch.setattr(httpx.AsyncClient, "request", _async_guard, raising=True)
    monkeypatch.setattr(httpx.Client, "request", _sync_guard, raising=True)

    yield


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Best-effort teardown for global DB engines.

    Prevents leaked SQLite/aiosqlite connections from throwing unraisable
    exceptions after the event loop has been closed.
    """
    try:
        import asyncio

        from myloware.storage.database import shutdown_async_db, shutdown_db

        shutdown_db()
        asyncio.run(shutdown_async_db())
    except Exception:
        # Never fail the test run during teardown.
        return


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio tests on asyncio (SQLAlchemy/asyncio-based stack)."""
    return "asyncio"


@pytest.fixture
def sample_brief() -> str:
    """Sample video brief for testing."""
    return "Create a 30-second video about AI technology trends"


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset rate limiter state between tests to avoid collision."""
    try:
        from myloware.api.server import limiter

        # Clear SlowAPI limiter storage before each test
        if hasattr(limiter, "_limiter") and limiter._limiter:
            storage = limiter._limiter.storage
            if hasattr(storage, "reset"):
                storage.reset()
            elif hasattr(storage, "storage"):
                # In-memory storage - clear the dict
                storage.storage.clear()
    except ImportError:
        pass  # server not imported yet

    yield

    # Clean up after test as well
    try:
        from myloware.api.server import limiter

        if hasattr(limiter, "_limiter") and limiter._limiter:
            storage = limiter._limiter.storage
            if hasattr(storage, "reset"):
                storage.reset()
            elif hasattr(storage, "storage"):
                storage.storage.clear()
    except ImportError:
        pass


@pytest.fixture
def api_headers() -> dict[str, str]:
    """Default API headers for authenticated endpoints."""
    from myloware.config import settings

    return {"X-API-Key": settings.api_key}


@pytest.fixture
async def async_client():
    """httpx AsyncClient wired to the FastAPI app with lifespan enabled."""
    from httpx import ASGITransport, AsyncClient

    from myloware.api.server import app

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
