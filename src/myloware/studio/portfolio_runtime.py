"""Fail-closed composition of the separately stored public portfolio library."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.config import settings
from myloware.config.studio import StudioSettings
from myloware.storage.studio_store import StudioError, StudioStore
from myloware.studio.portfolio_library_service import PortfolioLibraryService


def portfolio_library_errors(config: StudioSettings) -> tuple[str, ...]:
    public_errors = config.public_library_configuration_errors()
    if public_errors:
        return public_errors
    url = getattr(config, "library_database_url", None)
    root = getattr(config, "library_media_root", None)
    if not isinstance(url, str) or not url:
        return ("library_database_missing",)
    if not isinstance(root, Path):
        return ("library_media_root_missing",)
    # A separate URL is the cross-mode isolation boundary. Driver aliases are
    # normalized enough to reject the usual SQLite sync/async spelling pair.
    primary = str(settings.database_url)
    normalized = url.replace("+aiosqlite", "").replace("+asyncpg", "")
    if primary and normalized == primary.replace("+aiosqlite", "").replace("+asyncpg", ""):
        return ("library_database_must_be_separate",)
    return ()


def build_portfolio_library_service(store: StudioStore) -> PortfolioLibraryService:
    errors = portfolio_library_errors(store.config)
    if errors:
        raise StudioError(errors[0], 503)
    config = store.config
    url = str(config.library_database_url)
    root = Path(config.library_media_root)
    protected = tuple(
        path
        for path in (config.media_root, config.fixture_root, config.recorded_root)
        if path is not None
    )
    engine = create_async_engine(url)
    return PortfolioLibraryService(
        async_sessionmaker(engine, expire_on_commit=False),
        media_root=root,
        protected_roots=protected,
        source_factory=getattr(store, "factory", None),
    )


@asynccontextmanager
async def open_portfolio_library(store: StudioStore):
    """Compose one request/worker library service and dispose its engine."""
    service = build_portfolio_library_service(store)
    try:
        yield service
    finally:
        await service.factory.kw["bind"].dispose()
