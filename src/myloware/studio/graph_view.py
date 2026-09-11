"""Read-only, evidence-bound projection of the AISMR monthly LangGraph.

The Studio snapshot is a SQL projection of visitor-visible workflow state.  It
does not include a LangGraph checkpoint, so this module never infers one.  A
caller may supply an observed checkpoint receipt when it has one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

_NODE_SPECS: tuple[tuple[str, str, str | None], ...] = (
    ("ideate", "Create and safety-check the twelve-month idea plan.", None),
    ("review_ideas", "Pause for the visitor to review the idea plan.", "ideas"),
    (
        "generate",
        "Create durable media intents and submit the approved video and narration work.",
        None,
    ),
    ("wait_assets", "Pause while submitted media is collected and verified.", "assets"),
    ("edit", "Submit the verified media set to the ordered video render.", None),
    ("wait_render", "Pause while the render is inspected and verified.", "render"),
    ("review_final", "Pause for the visitor to review the verified final video.", "final"),
)

_TERMINAL = frozenset({"video_complete", "cancelled", "blocked", "failed", "submission_unknown"})
_EVENT_TYPES = frozenset(
    {
        "admitted",
        "ideation_submitting",
        "input_moderation_checked",
        "plan_moderation_checked",
        "plan_ready",
        "visitor_decision",
        "asset_intents_created",
        "assets_submitting",
        "assets_gathering",
        "assets_ready",
        "render_submitting",
        "render_queued",
        "render_running",
        "render_progress",
        "workflow_phase",
        "render_callback_received",
        "final_moderation_checked",
        "render_verified",
        "video_completed",
    }
)


def build_graph_view(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return seven native graph nodes derived only from a Studio snapshot.

    ``snapshot`` is expected to be the existing ``StudioStore.snapshot`` result.
    If it has an explicit ``checkpoint`` mapping, only a small safe subset is
    projected.  The normal snapshot has no checkpoint field.
    """
    status = _string(snapshot.get("status"))
    events = _event_types(snapshot.get("events"))
    assets = _asset_counts(snapshot.get("assets"))

    nodes = [
        _node("ideate", status, events, assets),
        _node("review_ideas", status, events, assets),
        _node("generate", status, events, assets),
        _node("wait_assets", status, events, assets),
        _node("edit", status, events, assets),
        _node("wait_render", status, events, assets),
        _node("review_final", status, events, assets),
    ]
    view: dict[str, Any] = {
        "kind": "source_derived",
        "source": "Studio run snapshot",
        "nodes": nodes,
        "checkpoint": _observed_checkpoint(snapshot),
    }
    return view


def _node(node_id: str, status: str, events: set[str], assets: Counter[str]) -> dict[str, Any]:
    purpose, interrupt = next(
        (purpose, boundary) for key, purpose, boundary in _NODE_SPECS if key == node_id
    )
    result: dict[str, Any] = {
        "id": node_id,
        "purpose": purpose,
        "state": _node_state(node_id, status, events),
        "interrupt": (
            {"native": True, "boundary": interrupt} if interrupt is not None else {"native": False}
        ),
    }
    if node_id == "generate":
        result["nested_work"] = {
            "kind": "custom_parallel_submission",
            "explanation": (
                "The generate node submits twelve video jobs and one narration batch. "
                "They are nested work, not twelve LangGraph branches."
            ),
            "video": {"expected": 12, "observed": assets["video"]},
            "narration_batch": {"expected": 1, "observed": assets["voice_batch"]},
        }
    if node_id in {"generate", "wait_assets", "edit", "wait_render"} and status in {
        "generating",
        "editing",
    }:
        result["resolution"] = (
            "coarse: Studio SQL status covers this adjacent node pair; events may show work, "
            "but do not by themselves establish the current LangGraph checkpoint."
        )
    return result


def _node_state(node_id: str, status: str, events: set[str]) -> str:
    if status == "ideating":
        return "active" if node_id == "ideate" else "pending"
    if status == "idea_review":
        return (
            "waiting"
            if node_id == "review_ideas"
            else ("completed" if node_id == "ideate" else "pending")
        )
    if status == "generating":
        if node_id in {"ideate", "review_ideas"}:
            return "completed"
        if node_id in {"generate", "wait_assets"}:
            return "coarse"
        return "pending"
    if status == "editing":
        if node_id in {"ideate", "review_ideas", "generate", "wait_assets"}:
            return "completed"
        if node_id in {"edit", "wait_render"}:
            return "coarse"
        return "pending"
    if status == "final_review":
        return "waiting" if node_id == "review_final" else "completed"
    if status == "video_complete":
        return "completed"
    if status in _TERMINAL:
        return "stopped"
    if "render_verified" in events and node_id != "review_final":
        return "completed"
    return "unknown"


def _event_types(value: object) -> set[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return set()
    return {
        _string(item.get("type"))
        for item in value
        if isinstance(item, Mapping) and _string(item.get("type")) in _EVENT_TYPES
    }


def _asset_counts(value: object) -> Counter[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return Counter()
    return Counter(_string(item.get("kind")) for item in value if isinstance(item, Mapping))


def _observed_checkpoint(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    value = snapshot.get("checkpoint")
    if not isinstance(value, Mapping):
        return None
    node_ids = {node[0] for node in _NODE_SPECS}
    result: dict[str, Any] = {"observed": True}
    thread_id = value.get("thread_id")
    if isinstance(thread_id, str) and thread_id.startswith("studio:"):
        result["thread_id"] = thread_id
    next_nodes = value.get("next")
    if isinstance(next_nodes, Sequence) and not isinstance(next_nodes, (str, bytes, bytearray)):
        safe_next = [node for node in next_nodes if isinstance(node, str) and node in node_ids]
        if safe_next:
            result["next"] = safe_next
        if len(safe_next) != len(next_nodes):
            result["mismatch"] = True
    return result


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""
