from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from myloware.api.routes.studio import get_store, router
from myloware.config.studio import StudioSettings
from myloware.storage.models import _utc_now
from myloware.storage.studio_store import StudioError
from myloware.studio.portfolio_library_service import PortfolioLibraryService, PublicFinal


@pytest.mark.asyncio
async def test_disabled_studio_returns_private_error_envelope() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: SimpleNamespace(
        config=StudioSettings(enabled=False)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/studio/config")
    assert response.status_code == 200
    assert response.json()["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("unlimited", [False, True])
async def test_config_reports_explicit_fixture_admission_override(unlimited: bool) -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: SimpleNamespace(
        config=StudioSettings(enabled=False, fixture_unlimited_admissions=unlimited)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/studio/config")
    assert response.status_code == 200
    assert response.json()["limits"]["fixture_unlimited"] is unlimited


@pytest.mark.asyncio
async def test_session_requires_exact_origin() -> None:
    settings = StudioSettings(enabled=True)

    class Store:
        config = settings

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: Store()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/studio/session", json={}, headers={"Origin": "https://evil.example"}
        )
    assert response.status_code == 403
    assert response.json() == {"error": "origin_required"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "suffix", "cookie_name"),
    [
        ("http://127.0.0.1:8351", None, "aismr_session_8351"),
        ("https://studio.example.test:8452", "live_trial", "aismr_session_live_trial"),
    ],
)
async def test_alternate_and_named_sessions_stay_isolated(
    origin: str, suffix: str | None, cookie_name: str
) -> None:
    settings = StudioSettings(
        enabled=True,
        origin=origin,
        cookie_secure=origin.startswith("https:"),
        session_cookie_suffix=suffix,
    )

    class Store:
        config = settings

        def csrf(self, visitor_id: str) -> str:
            return f"csrf-{visitor_id}"

        async def visitor(self, cookie: str | None):
            if cookie != "port-session":
                raise StudioError("session_required", 401)
            return SimpleNamespace(id="port-visitor")

        async def latest_run(self, _visitor_id: str):
            return None

        async def new_visitor(self, _ip: str):
            return SimpleNamespace(id="port-visitor"), "port-session"

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: Store()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=settings.origin) as client:
        created = await client.post(
            "/v1/studio/session", json={}, headers={"Origin": settings.origin}
        )
        legacy = await client.get(
            "/v1/studio/session", headers={"Cookie": "aismr_session=other-instance"}
        )
        isolated = await client.get(
            "/v1/studio/session", headers={"Cookie": f"{cookie_name}=port-session"}
        )

    assert f"{cookie_name}=port-session" in created.headers["set-cookie"]
    if settings.cookie_secure:
        assert "Secure" in created.headers["set-cookie"]
    assert legacy.status_code == 401
    assert isolated.status_code == 200 and isolated.json()["visitor_id"] == "port-visitor"


def test_canonical_and_public_origins_keep_the_existing_session_cookie_name() -> None:
    assert StudioSettings().session_cookie_name == "aismr_session"
    assert StudioSettings(
        origin="https://studio.example.test", cookie_secure=True
    ).session_cookie_name == ("aismr_session")


@pytest.mark.parametrize("suffix", ["", "live; Path=/", "live\r\n", "a" * 33])
def test_session_cookie_suffix_rejects_invalid_cookie_name_parts(suffix: str) -> None:
    with pytest.raises(ValueError):
        StudioSettings(session_cookie_suffix=suffix)


@pytest.mark.asyncio
async def test_unassembled_live_runtime_refuses_admission_before_storage() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: SimpleNamespace(
        config=SimpleNamespace(enabled=True, mode="live")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/studio/runs", json={"item": "teacup", "request_key": "live-pending"}
        )
    assert response.status_code == 503
    assert response.json() == {"error": "live_runtime_not_ready"}


@pytest.mark.asyncio
async def test_gallery_reads_separate_library_when_generation_is_disabled(tmp_path: Path) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    library_url = f"sqlite+aiosqlite:///{tmp_path / 'gallery.db'}"
    root = tmp_path / "portfolio-media"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"recorded final")
    service = PortfolioLibraryService(
        async_sessionmaker(create_async_engine(library_url), expire_on_commit=False),
        media_root=root,
        protected_roots=(tmp_path / "mode-media",),
    )
    await service.initialize()
    payload = source.read_bytes()
    entry = await service.accept(
        PublicFinal(
            source_instance_key="recorded:seed:1:" + sha256(payload).hexdigest(),
            source_run_id=uuid4(),
            source_revision=1,
            source_mode="recorded",
            item_label="Teacup",
            source_final=source,
            final_sha256=sha256(payload).hexdigest(),
            accepted_at=_utc_now(),
            public_suitability_receipt="verified-suitability",
            rights_receipt="verified-rights",
            consent_subject_hash="a" * 64,
            history={
                "months": 12,
                "rights_profile_expires_at": "2099-01-01T00:00:00+00:00",
                "rights_profile_version": "test-v1",
                "rights_profile_receipt_id": "rights-id",
                "suitability_expires_at": "2099-01-01T00:00:00+00:00",
                "suitability_receipt_id": "suitability-id",
            },
        )
    )
    config = StudioSettings(
        enabled=False,
        library_database_url=library_url,
        library_media_root=root,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: SimpleNamespace(config=config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/studio/gallery")
        assert response.status_code == 200 and response.json()["items"][0]["id"] == str(entry.id)
        video = await client.get(
            f"/v1/studio/gallery/{entry.id}/video", headers={"Range": "bytes=0-7"}
        )
    assert video.status_code == 206 and video.content == payload[:8]
