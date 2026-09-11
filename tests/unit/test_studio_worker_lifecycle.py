from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from myloware.storage.studio_store import StudioError
from myloware.workers import handlers


@pytest.mark.asyncio
async def test_studio_advance_error_recovery_closes_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    stopped: list[tuple[object, str]] = []

    class Session:
        rolled_back = False

        async def rollback(self) -> None:
            self.rolled_back = True

    class Store:
        async def stop(self, received_run_id: object, code: str) -> None:
            stopped.append((received_run_id, code))

    class Service:
        def __init__(self) -> None:
            self.store = Store()
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    service = Service()
    session = Session()

    async def fail_advance(_run_id: object) -> bool:
        raise StudioError("stage_failed")

    monkeypatch.setattr(
        "myloware.workflows.langgraph.studio.advance_monthly_workflow", fail_advance
    )
    monkeypatch.setattr("myloware.studio.service.build_studio_service", lambda: service)

    await handlers.handle_job(
        job_type="studio.advance",
        run_id=run_id,
        payload={},
        session_run_repo=object(),
        session_artifact_repo=object(),
        session_job_repo=SimpleNamespace(session=session),
        llama_client=object(),
    )

    assert session.rolled_back is True
    assert stopped == [(run_id, "stage_failed")]
    assert service.closed is True
