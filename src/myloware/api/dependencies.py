"""Common FastAPI dependencies for the AISMR API."""

from __future__ import annotations

from typing import Generator

from fastapi import Depends, Request, HTTPException, Security
from fastapi.security import APIKeyHeader
from llama_stack_client import AsyncLlamaStackClient, LlamaStackClient
from sqlalchemy.orm import Session

from myloware.config import settings
from myloware.llama_clients import get_async_client, get_sync_client
from myloware.storage.database import get_session
from myloware.storage.repositories import (
    ArtifactRepository,
    ChatSessionRepository,
    FeedbackRepository,
    RunRepository,
)

__all__ = [
    "get_db_session",
    "get_run_repo",
    "get_artifact_repo",
    "get_chat_session_repo",
    "get_feedback_repo",
    "get_llama_client",
    "get_async_llama_client",
    "get_vector_db_id",
    "verify_api_key",
    "api_key_header",
]


def get_db_session() -> Generator[Session, None, None]:
    """Provide a SQLAlchemy session for request scope."""

    with get_session() as session:
        yield session


def get_run_repo(session: Session = Depends(get_db_session)) -> RunRepository:
    """Dependency that returns a RunRepository bound to the session."""

    return RunRepository(session)


def get_artifact_repo(session: Session = Depends(get_db_session)) -> ArtifactRepository:
    """Dependency that returns an ArtifactRepository bound to the session."""

    return ArtifactRepository(session)


def get_chat_session_repo(session: Session = Depends(get_db_session)) -> ChatSessionRepository:
    """Dependency that returns a ChatSessionRepository bound to the session."""

    return ChatSessionRepository(session)


def get_feedback_repo(session: Session = Depends(get_db_session)) -> FeedbackRepository:
    """Dependency that returns a FeedbackRepository bound to the session."""

    return FeedbackRepository(session)


def get_llama_client() -> LlamaStackClient:
    """Provide a cached Llama Stack client."""

    return get_sync_client()


def get_async_llama_client() -> AsyncLlamaStackClient:
    """Provide a cached async Llama Stack client."""

    return get_async_client()


def get_vector_db_id(request: Request) -> str:
    """Return the registered vector DB identifier from app state."""

    return getattr(request.app.state, "vector_db_id", "project_kb_myloware")


# API key verification (shared)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    """Verify API key authentication for all endpoints."""

    if api_key is None:
        raise HTTPException(status_code=401, detail="Missing API key")
    if api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key
