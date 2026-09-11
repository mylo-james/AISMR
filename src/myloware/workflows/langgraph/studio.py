"""Monthly LangGraph stages using the shared durable engine and SQL job queue.

The graph coordinates stage transitions. SQL owns media receipts, admission and
visitor decisions. Resume messages are wake-ups, never approval credentials.
"""

from __future__ import annotations

from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from myloware.storage.models import _utc_now
from myloware.storage.studio_store import TERMINAL, StudioError
from myloware.studio.service import StudioService, build_studio_service, open_studio_service


class MonthlyGraphState(TypedDict, total=False):
    run_id: str
    status: str
    revision: int


async def _snapshot(service: StudioService, run_id: UUID) -> dict[str, Any]:
    run = await service.store.get_run(run_id)
    return {"run_id": str(run_id), "status": run.status, "revision": run.revision}


async def ideate(state: MonthlyGraphState) -> dict[str, Any]:
    run_id = UUID(state["run_id"])
    async with open_studio_service(build_studio_service) as service:
        await service.ideate(run_id)
        return await _snapshot(service, run_id)


async def review_ideas(state: MonthlyGraphState) -> dict[str, Any]:
    interrupt({"gate": "ideas", "revision": state["revision"]})
    async with open_studio_service(build_studio_service) as service:
        return await _snapshot(service, UUID(state["run_id"]))


async def generate(state: MonthlyGraphState) -> dict[str, Any]:
    run_id = UUID(state["run_id"])
    async with open_studio_service(build_studio_service) as service:
        run = await service.store.get_run(run_id)
        if run.mode == "planning":
            raise StudioError("planning_only", 409)
        await service.assets(run_id, run=run).submit(run_id)
        return await _snapshot(service, run_id)


async def wait_assets(state: MonthlyGraphState) -> dict[str, Any]:
    interrupt({"stage": "assets"})
    run_id = UUID(state["run_id"])
    async with open_studio_service(build_studio_service) as service:
        run = await service.store.get_run(run_id)
        await service.assets(run_id, run=run).reconcile(run_id)
        return await _snapshot(service, run_id)


async def edit(state: MonthlyGraphState) -> dict[str, Any]:
    run_id = UUID(state["run_id"])
    async with open_studio_service(build_studio_service) as service:
        await service.submit_edit(run_id)
        return await _snapshot(service, run_id)


async def wait_render(state: MonthlyGraphState) -> dict[str, Any]:
    interrupt({"stage": "render"})
    run_id = UUID(state["run_id"])
    async with open_studio_service(build_studio_service) as service:
        await service.inspect_edit(run_id)
        return await _snapshot(service, run_id)


async def review_final(state: MonthlyGraphState) -> dict[str, Any]:
    interrupt({"gate": "final", "revision": state["revision"]})
    async with open_studio_service(build_studio_service) as service:
        return await _snapshot(service, UUID(state["run_id"]))


def _route(state: MonthlyGraphState, allowed: dict[str, str]) -> str:
    return allowed.get(state.get("status", ""), END)


def _retryable_wait_render(snapshot: Any, run: Any) -> bool:
    """Allow LangGraph's native retry only for a proved, already accepted render."""
    if (
        run.status != "editing"
        or not run.render_job_id
        or not run.render_input_hash
        or run.render_submission_state not in {"accepted", "running"}
    ):
        return False
    tasks = getattr(snapshot, "tasks", ())
    return any(
        getattr(task, "name", None) == "wait_render" and getattr(task, "error", None)
        for task in tasks
    )


def _runtime_mode_compatible(*, configured_mode: str, run_mode: str) -> bool:
    """Permit only the effect-free legacy planning path under local runtime."""
    return configured_mode == run_mode or (configured_mode == "local" and run_mode == "planning")


def build_monthly_graph(checkpointer: Any) -> Any:
    graph = StateGraph(MonthlyGraphState)
    for name, function in {
        "ideate": ideate,
        "review_ideas": review_ideas,
        "generate": generate,
        "wait_assets": wait_assets,
        "edit": edit,
        "wait_render": wait_render,
        "review_final": review_final,
    }.items():
        graph.add_node(name, function)
    graph.add_edge(START, "ideate")
    graph.add_conditional_edges("ideate", lambda s: _route(s, {"idea_review": "review_ideas"}))
    graph.add_conditional_edges(
        "review_ideas",
        lambda s: _route(
            s,
            {
                "ideating": "ideate",
                "generating": "generate",
                "idea_review": "review_ideas",
            },
        ),
    )
    graph.add_conditional_edges(
        "generate",
        lambda s: _route(s, {"generating": "wait_assets", "editing": "edit"}),
    )
    graph.add_conditional_edges(
        "wait_assets",
        lambda s: _route(s, {"generating": "wait_assets", "editing": "edit"}),
    )
    graph.add_conditional_edges(
        "edit",
        lambda s: _route(s, {"editing": "wait_render", "final_review": "review_final"}),
    )
    graph.add_conditional_edges(
        "wait_render",
        lambda s: _route(s, {"editing": "wait_render", "final_review": "review_final"}),
    )
    graph.add_conditional_edges(
        "review_final",
        lambda s: _route(s, {"final_review": "review_final"}),
    )
    return graph.compile(checkpointer=checkpointer)


async def advance_monthly_workflow(run_id: UUID) -> bool:
    """Perform one durable queue wake-up; return true only when work is terminal."""
    from myloware.workflows.langgraph.graph import get_langgraph_engine

    async with open_studio_service(build_studio_service) as service:
        run = await service.store.get_run(run_id)
        from myloware.storage.studio_models import StudioPlannerRun

        async with service.store.factory() as session:
            planner = await session.get(StudioPlannerRun, run_id)
        if (
            planner
            and planner.configuration.get("recorded_final_reuse")
            and (
                not service.config.recorded_final_reuse
                or planner.configuration.get("bundle_sha256")
                != service.config.recorded_bundle_sha256
            )
        ):
            await service.store.stop(run_id, "recorded_bundle_changed")
            return True
        if run.status in TERMINAL or run.status == "submission_unknown":
            return True
        if not _runtime_mode_compatible(configured_mode=service.config.mode, run_mode=run.mode):
            # A restart with a different provider mode must never change effects
            # for a previously admitted run. Local runtime may only service an
            # existing planning run because both paths retain the planning-only
            # effect boundary and use the same real text composition.
            await service.store.stop(run_id, "runtime_mode_mismatch")
            return True
        if run.expires_at <= _utc_now():
            await service.store.stop(run_id, "run_expired")
            return True
    engine = get_langgraph_engine()
    await engine.ensure_checkpointer_initialized()
    graph = engine.get_monthly_graph()
    config = {"configurable": {"thread_id": f"studio:{run_id}"}, "recursion_limit": 40}
    snapshot = await graph.aget_state(config)
    if snapshot.next and (
        ("review_ideas" in snapshot.next and run.status == "idea_review")
        or ("review_final" in snapshot.next and run.status == "final_review")
    ):
        return False
    if not snapshot.values:
        await graph.ainvoke(
            {"run_id": str(run_id), "status": run.status, "revision": run.revision},
            config,
        )
    elif snapshot.next:
        await graph.ainvoke(Command(resume={"wake": True}), config)
    elif _retryable_wait_render(snapshot, run):
        # LangGraph retains the failed task. Native `None` invocation retries
        # that task only, without replaying earlier generation or render submit.
        await graph.ainvoke(None, config)
    elif run.status == "ideating" and any(
        getattr(task, "name", None) == "ideate" and getattr(task, "error", None)
        for task in getattr(snapshot, "tasks", ())
    ):
        # Only v2's persisted operation receipts support a safe planning replay.
        from myloware.studio.planning_store import planner_version

        async with open_studio_service(build_studio_service) as service:
            if await planner_version(service.store, run_id) != "creative-v2":
                await service.store.stop(run_id, "workflow_checkpoint_mismatch")
                return True
        await graph.ainvoke(None, config)
    else:
        # A finished checkpoint with nonterminal SQL state is an inconsistency,
        # never an excuse to replay paid effects from the beginning.
        async with open_studio_service(build_studio_service) as service:
            await service.store.stop(run_id, "workflow_checkpoint_mismatch")
    async with open_studio_service(build_studio_service) as service:
        current = await service.store.get_run(run_id)
        return current.status in TERMINAL or current.status == "submission_unknown"
