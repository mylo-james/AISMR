from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from myloware.config.studio import StudioSettings
from myloware.studio.service import StudioService
from myloware.workflows.langgraph.studio import _retryable_wait_render


def test_only_failed_wait_render_with_accepted_receipt_is_retryable() -> None:
    run = SimpleNamespace(
        status="editing",
        render_job_id="job",
        render_input_hash="a" * 64,
        render_submission_state="accepted",
    )
    failed_render = SimpleNamespace(
        tasks=(SimpleNamespace(name="wait_render", error="MediaVerificationError"),)
    )
    assert _retryable_wait_render(failed_render, run)


def test_unknown_or_effectful_nodes_are_never_retryable() -> None:
    run = SimpleNamespace(
        status="editing",
        render_job_id="job",
        render_input_hash="a" * 64,
        render_submission_state="accepted",
    )
    assert not _retryable_wait_render(
        SimpleNamespace(tasks=(SimpleNamespace(name="generate", error="boom"),)), run
    )
    assert not _retryable_wait_render(
        SimpleNamespace(tasks=(SimpleNamespace(name="wait_render", error="boom"),)),
        SimpleNamespace(
            status="generating",
            render_job_id="job",
            render_input_hash="a" * 64,
            render_submission_state="accepted",
        ),
    )


@pytest.mark.asyncio
async def test_langgraph_retries_only_failed_wait_render_from_real_checkpoint() -> None:
    calls = {"inspect": 0, "submit": 0}

    async def wait_render(state):
        calls["inspect"] += 1
        if calls["inspect"] == 1:
            raise RuntimeError("MediaVerificationError")
        return {"status": "final_review"}

    async def submit_edit(_state):
        calls["submit"] += 1
        return {"status": "editing"}

    graph = StateGraph(dict)
    graph.add_node("wait_render", wait_render)
    graph.add_node("edit", submit_edit)
    graph.add_edge(START, "wait_render")
    graph.add_edge("wait_render", END)
    compiled = graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "render-retry"}}
    with pytest.raises(RuntimeError, match="MediaVerificationError"):
        await compiled.ainvoke({"status": "editing"}, config)
    failed = await compiled.aget_state(config)
    assert "wait_render" in failed.next and any(
        task.name == "wait_render" and task.error for task in failed.tasks
    )
    await compiled.ainvoke(None, config)
    recovered = await compiled.aget_state(config)
    assert recovered.values["status"] == "final_review"
    assert calls == {"inspect": 2, "submit": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("poll_result", "code", "status"),
    [
        ({"status": "rejected", "media_verified": False}, "render_rejected", "failed"),
        (
            {"status": "unknown", "media_verified": False},
            "render_receipt_unknown",
            "submission_unknown",
        ),
        (
            {"status": "ready", "media_verified": False},
            "render_result_unverified",
            "submission_unknown",
        ),
    ],
)
async def test_terminal_or_unverified_render_poll_stops_without_resubmission(
    monkeypatch: pytest.MonkeyPatch, poll_result, code: str, status: str
) -> None:  # type: ignore[no-untyped-def]
    run_id = "a" * 32
    stopped: list[tuple[object, str, str]] = []

    class Store:
        config = StudioSettings()

        async def get_run(self, _run_id):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                mode="fixture",
                status="editing",
                render_job_id="render-job",
                render_input_hash="b" * 64,
            )

        async def stop(self, received_run_id, received_code, *, status):  # type: ignore[no-untyped-def]
            stopped.append((received_run_id, received_code, status))

    async def poll(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return poll_result

    monkeypatch.setattr("myloware.studio.service.MonthlyEditor.poll", poll)
    await StudioService(Store(), moderator=SimpleNamespace()).inspect_edit(run_id)  # type: ignore[arg-type]

    assert stopped == [(run_id, code, status)]
