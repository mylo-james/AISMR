"""Database connection and session management."""

from __future__ import annotations

from contextlib import contextmanager
import asyncio
from typing import Generator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool, NullPool

from myloware.config import settings
from myloware.observability.logging import get_logger
from myloware.storage.models import Base
from myloware.storage import studio_models  # noqa: F401 - register additive studio tables

logger = get_logger(__name__)

__all__ = [
    "get_engine",
    "get_session",
    "init_db",
    "shutdown_db",
    "get_session_factory",
    "get_async_engine",
    "get_async_session_factory",
    "init_async_db",
    "shutdown_async_db",
]

_engine: Engine | None = None
_engine_url: str | None = None
_SessionLocal: sessionmaker[Session] | None = None
_engines_to_dispose: dict[int, Engine] = {}

_async_engine: AsyncEngine | None = None
_async_engine_url: str | None = None
_async_engine_loop_id: int | None = (
    None  # Track loop where engine was created to avoid cross-loop reuse
)
_AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None
_async_engines_to_dispose: dict[int, AsyncEngine] = {}


def _stash_engine(engine: Engine) -> None:
    """Keep a strong ref until shutdown to avoid leaked connections/threads."""
    _engines_to_dispose[id(engine)] = engine


def _stash_async_engine(engine: AsyncEngine) -> None:
    """Keep a strong ref until shutdown to avoid leaked aiosqlite threads in tests."""
    _async_engines_to_dispose[id(engine)] = engine


def _is_sqlite_file(url: str) -> bool:
    return url.startswith("sqlite") and ":memory:" not in url


def get_engine() -> Engine:
    """Get or create the database engine."""
    global _engine, _engine_url, _SessionLocal
    if _engine is None or _engine_url != settings.database_url:
        if _engine is not None:
            _stash_engine(_engine)
            try:
                _engine.dispose()
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("Failed to dispose sync engine on url change", exc_info=True)

        logger.info("Creating database engine")
        url = settings.database_url
        # Convert aiosqlite URLs to regular SQLite for sync sessions
        if url.startswith("sqlite+aiosqlite://"):
            url = url.replace("sqlite+aiosqlite://", "sqlite://")
        kw = {"pool_pre_ping": True}
        # SQLite specifics
        if url.startswith("sqlite"):
            kw.setdefault("connect_args", {})
            # Allow connections to be used across threads (TestClient + background tasks)
            kw["connect_args"].setdefault("check_same_thread", False)
        else:
            # SQLite doesn't support pool_size/max_overflow
            kw["pool_size"] = settings.db_pool_size
            kw["max_overflow"] = settings.db_max_overflow
        _engine = create_engine(url, **kw)
        _engine_url = settings.database_url  # Store original URL for comparison
        _SessionLocal = None
    return _engine


def get_async_engine() -> AsyncEngine:
    """Get or create async engine."""
    global _async_engine, _async_engine_url, _AsyncSessionLocal, _async_engine_loop_id

    def _current_loop_id() -> int | None:
        try:
            return id(asyncio.get_running_loop())
        except RuntimeError:
            return None

    curr_loop_id = _current_loop_id()
    url = settings.database_url
    if url.startswith("postgresql+psycopg2://"):
        url = url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://")
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://")
    # Ensure SQLite URLs use aiosqlite driver for async
    if url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://"):
        url = url.replace("sqlite://", "sqlite+aiosqlite://")

    # If engine was created on a different event loop, discard and rebuild to avoid
    # asyncpg cross-loop errors like "Future attached to a different loop".
    loop_mismatch = (
        _async_engine is not None
        and _async_engine_loop_id is not None
        and curr_loop_id is not None
        and curr_loop_id != _async_engine_loop_id
    )
    if loop_mismatch:
        try:
            old_engine = _async_engine
            if old_engine is not None:
                _stash_async_engine(old_engine)
                old_engine.sync_engine.dispose()
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.debug("Failed to dispose async engine on loop mismatch", exc_info=True)
        _async_engine = None
        _AsyncSessionLocal = None
        _async_engine_url = None
        _async_engine_loop_id = None

    if _async_engine is not None and _async_engine_url != url:
        _stash_async_engine(_async_engine)
        _async_engine = None
        _AsyncSessionLocal = None
        _async_engine_url = None
        _async_engine_loop_id = None

    if _async_engine is None or _async_engine_url != url:
        logger.info("Creating async database engine")
        kw = {"pool_pre_ping": True}

        # SQLite specifics: share in-memory DB across connections
        if url.startswith("sqlite"):
            kw.setdefault("connect_args", {})
            kw["connect_args"].setdefault("check_same_thread", False)
            # WAL mode is enabled in init_async_db() via PRAGMA statements
            # SQLAlchemy doesn't support setting PRAGMA in connect_args directly
            if ":memory:" in url:
                kw["poolclass"] = StaticPool
            elif settings.environment == "test":
                # pytest-asyncio uses per-test event loops by default; pooling can leak aiosqlite
                # threads across loop teardown and cause the interpreter to hang on exit.
                kw["poolclass"] = NullPool
        else:
            # Default: prefer stability over pooling across event loops
            use_pool = settings.async_use_pool and not loop_mismatch
            if not use_pool:
                kw["poolclass"] = NullPool
            else:
                kw["pool_size"] = settings.db_pool_size
                kw["max_overflow"] = settings.db_max_overflow

        _async_engine = create_async_engine(url, **kw)
        _async_engine_url = url
        _async_engine_loop_id = curr_loop_id
        _AsyncSessionLocal = None
    return _async_engine


def get_session_factory() -> sessionmaker[Session]:
    """Get or create the session factory (sync)."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            bind=get_engine(),
        )
    return _SessionLocal


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create async session factory."""
    global _AsyncSessionLocal
    if _AsyncSessionLocal is None:
        _AsyncSessionLocal = async_sessionmaker(
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            bind=get_async_engine(),
            class_=AsyncSession,
        )
    return _AsyncSessionLocal


async def init_async_db() -> None:
    """Initialize database tables using the async engine.

    Ensures in-memory SQLite databases are created before first request
    and keeps behavior consistent with sync init_db.
    """
    # Recreate the async engine for the current settings to avoid cross-loop reuse in tests,
    # but always dispose any prior engines to prevent leaked aiosqlite threads.
    await shutdown_async_db()
    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Enable WAL mode and related pragmas for file-based SQLite (better concurrency)
        if _is_sqlite_file(settings.database_url):
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.execute(text("PRAGMA busy_timeout=3000"))
            logger.info("Enabled WAL mode for SQLite database")
    logger.info("Async database tables created")


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager that yields a database session."""
    SessionLocal = get_session_factory()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Initialize database tables."""
    logger.info("Initializing database tables")
    engine = get_engine()
    Base.metadata.create_all(bind=engine)

    # Enable WAL for SQLite files to reduce writer/read contention with async sessions
    if _is_sqlite_file(settings.database_url):
        try:
            with engine.begin() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))
                conn.execute(text("PRAGMA synchronous=NORMAL"))
                conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.execute(text("PRAGMA busy_timeout=3000"))
            logger.info("Enabled WAL mode for SQLite database (sync engine)")
        except Exception:
            logger.warning("Could not enable WAL mode for SQLite sync engine", exc_info=True)

    logger.info("Database tables created")


def shutdown_db() -> None:
    """Dispose the sync engine and clear session factory caches.

    This is primarily used by test teardown to avoid leaking open connections.
    """
    global _engine, _engine_url, _SessionLocal, _engines_to_dispose
    if _SessionLocal is not None:
        _SessionLocal = None
    if _engine is not None:
        _stash_engine(_engine)

    engines = list(_engines_to_dispose.values())
    _engines_to_dispose = {}
    for engine in engines:
        try:
            engine.dispose()
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.debug("Failed to dispose sync engine", exc_info=True)

    _engine = None
    _engine_url = None


async def shutdown_async_db() -> None:
    """Dispose the async engine and clear session factory caches.

    This is important for SQLite/aiosqlite in tests: leaked connections can
    cause unraisable exceptions at interpreter shutdown when the event loop
    has already been closed.
    """
    global _async_engine, _async_engine_url, _AsyncSessionLocal, _async_engine_loop_id
    global _async_engines_to_dispose

    if _AsyncSessionLocal is not None:
        _AsyncSessionLocal = None

    if _async_engine is not None:
        _stash_async_engine(_async_engine)

    engines = list(_async_engines_to_dispose.values())
    _async_engines_to_dispose = {}
    for engine in engines:
        try:
            await engine.dispose()
        except Exception:  # pragma: no cover - best-effort cleanup
            try:
                engine.sync_engine.dispose()
            except Exception:
                logger.debug("Failed to dispose async engine", exc_info=True)

    _async_engine = None
    _async_engine_url = None
    _async_engine_loop_id = None
