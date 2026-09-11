from __future__ import annotations

from myloware.studio.graph_view import build_graph_view


def _by_id(view: dict[str, object], node_id: str) -> dict[str, object]:
    nodes = view["nodes"]
    assert isinstance(nodes, list)
    return next(node for node in nodes if node["id"] == node_id)


def test_graph_view_exposes_the_seven_monthly_native_nodes() -> None:
    view = build_graph_view({"status": "idea_review", "events": [], "assets": []})

    assert [node["id"] for node in view["nodes"]] == [
        "ideate",
        "review_ideas",
        "generate",
        "wait_assets",
        "edit",
        "wait_render",
        "review_final",
    ]
    assert _by_id(view, "ideate")["state"] == "completed"
    assert _by_id(view, "review_ideas")["state"] == "waiting"
    assert _by_id(view, "review_ideas")["interrupt"] == {"native": True, "boundary": "ideas"}
    assert _by_id(view, "generate")["interrupt"] == {"native": False}
    assert view["checkpoint"] is None


def test_graph_view_keeps_parallel_assets_nested_under_generate() -> None:
    view = build_graph_view(
        {
            "status": "generating",
            "events": [{"type": "asset_intents_created"}],
            "assets": [{"kind": "video"}] * 12 + [{"kind": "voice_batch"}],
        }
    )

    generate = _by_id(view, "generate")
    assert generate["state"] == "coarse"
    assert generate["nested_work"] == {
        "kind": "custom_parallel_submission",
        "explanation": (
            "The generate node submits twelve video jobs and one narration batch. "
            "They are nested work, not twelve LangGraph branches."
        ),
        "video": {"expected": 12, "observed": 12},
        "narration_batch": {"expected": 1, "observed": 1},
    }
    assert _by_id(view, "wait_assets")["state"] == "coarse"
    assert "coarse" in str(_by_id(view, "wait_assets")["resolution"])


def test_graph_view_only_reports_an_explicit_checkpoint_receipt() -> None:
    without_checkpoint = build_graph_view({"status": "editing", "events": [], "assets": []})
    with_checkpoint = build_graph_view(
        {
            "status": "editing",
            "events": [],
            "assets": [],
            "checkpoint": {"next": ["wait_render"], "thread_id": "studio:example"},
        }
    )

    assert without_checkpoint["checkpoint"] is None
    assert with_checkpoint["checkpoint"] == {
        "observed": True,
        "next": ["wait_render"],
        "thread_id": "studio:example",
    }
    assert _by_id(with_checkpoint, "edit")["state"] == "coarse"
    assert _by_id(with_checkpoint, "wait_render")["state"] == "coarse"


def test_graph_view_drops_raw_checkpoint_values_and_marks_unknown_node_mismatch() -> None:
    view = build_graph_view(
        {
            "status": "editing",
            "events": [],
            "assets": [],
            "checkpoint": {
                "thread_id": "unrelated-thread",
                "next": ["wait_render", "untrusted_node"],
                "provider_payload": {"secret": "must-not-leak"},
            },
        }
    )

    assert view["checkpoint"] == {"observed": True, "next": ["wait_render"], "mismatch": True}
