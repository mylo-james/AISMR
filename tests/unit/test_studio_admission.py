from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from myloware.agents.tools.supervisor import ApproveGateTool
from myloware.storage.models import Base
from myloware.storage.repositories import RunRepository
from myloware.workflows.admission import (
    StudioAdmissionRequiredError,
    require_legacy_monthly_engine_allowed,
)


def _repository() -> RunRepository:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return RunRepository(sessionmaker(bind=engine)())


def test_legacy_run_creation_is_blocked_when_aismr_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISMR_ENABLED", "true")
    with pytest.raises(StudioAdmissionRequiredError, match="studio_admission_required"):
        _repository().create("aismr", "brief")


def test_legacy_run_creation_is_unchanged_when_aismr_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISMR_ENABLED", "false")
    run = _repository().create("aismr", "brief")
    assert run.workflow_name == "aismr"


@pytest.mark.asyncio
async def test_legacy_async_run_creation_is_blocked_when_aismr_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISMR_ENABLED", "true")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine)() as session:
            with pytest.raises(StudioAdmissionRequiredError, match="studio_admission_required"):
                await RunRepository(session).create_async("aismr", "brief")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_async_run_creation_is_unchanged_when_aismr_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISMR_ENABLED", "false")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine)() as session:
            run = await RunRepository(session).create_async("aismr", "brief")
            assert run.workflow_name == "aismr"
    finally:
        await engine.dispose()


def test_legacy_monthly_engine_entry_is_blocked_when_aismr_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISMR_ENABLED", "true")
    with pytest.raises(StudioAdmissionRequiredError, match="studio_admission_required"):
        require_legacy_monthly_engine_allowed("monthly")


def test_supervisor_cannot_approve_a_monthly_visitor_gate() -> None:
    run_id = uuid4()
    called = False

    class Repository:
        def get(self, requested_id):  # type: ignore[no-untyped-def]
            assert requested_id == run_id
            return SimpleNamespace(id=run_id, workflow_name="monthly")

    def approval(**_kwargs):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        raise AssertionError("monthly visitor decision must not be created by the supervisor")

    tool = ApproveGateTool(
        client_factory=lambda: object(),
        run_repo_factory=Repository,
        artifact_repo_factory=lambda: object(),
        gate_approver=approval,
    )
    assert tool.run_impl(str(run_id), "publish") == {
        "run_id": str(run_id),
        "status": "pending_visitor_action",
        "current_step": "visitor_decision",
    }
    assert called is False
