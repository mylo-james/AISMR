from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from myloware.studio.telemetry import LoadedKnowledgeSource, project_telemetry, public_events


def _event(sequence: int, stage: str, event_type: str, at: datetime, detail=None):
    return SimpleNamespace(
        sequence=sequence, stage=stage, event_type=event_type, created_at=at, detail=detail or {}
    )


def test_public_events_allow_only_documented_types_and_scalar_detail() -> None:
    now = datetime(2026, 9, 9, tzinfo=UTC)
    rows = public_events(
        [
            _event(
                1,
                "generating",
                "asset_intents_created",
                now,
                {"count": 24, "prompt": "secret", "nested": {"key": "no"}},
            ),
            _event(2, "generating", "provider_payload", now, {"token": "secret"}),
        ]
    )
    assert rows == [
        {
            "sequence": 1,
            "occurred_at": "2026-09-09T00:00:00+00:00Z",
            "type": "asset_intents_created",
            "explanation": "Durable media job intents were created before provider submission.",
            "subject": "generating",
            "detail": {"count": 24},
        }
    ]


def test_public_events_allows_scene_count_without_exposing_other_detail() -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    rows = public_events(
        [_event(1, "ideating", "plan_ready", now, {"scenes": 12, "plan": "private"})]
    )
    assert rows[0]["detail"] == {"scenes": 12}
    assert rows[0]["explanation"] == "A twelve-scene plan was durably saved for visitor review."


def test_projection_counts_work_and_uses_event_wall_clock_only() -> None:
    start = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)
    source = LoadedKnowledgeSource(
        source="data/knowledge/ideation/concept-planning.md", text="Context"
    )
    events = [
        _event(1, "ideating", "admitted", start),
        _event(
            2,
            "ideating",
            "knowledge_loaded",
            start + timedelta(seconds=4),
            {"sources": [source.public()]},
        ),
        _event(
            3, "generating", "asset_intents_created", start + timedelta(seconds=9), {"count": 24}
        ),
    ]
    assets = [
        SimpleNamespace(status=status)
        for status in (
            "queued",
            "submitting",
            "running",
            "ready",
            "gathered",
            "failed",
            "submission_unknown",
        )
    ]
    telemetry = project_telemetry(
        events=events,
        assets=assets,
        run=SimpleNamespace(mode="fixture", final_metadata={}),
    )
    assert telemetry["work"] == {
        "queued_count": 1,
        "submitting_count": 1,
        "running_count": 1,
        "completed_count": 2,
        "failed_count": 2,
    }
    assert telemetry["stage_timings"] == [
        {
            "stage": "ideating",
            "started_at": "2026-09-09T10:00:00+00:00Z",
            "ended_at": "2026-09-09T10:00:09+00:00Z",
            "elapsed_seconds": 9.0,
        },
        {
            "stage": "generating",
            "started_at": "2026-09-09T10:00:09+00:00Z",
            "ended_at": None,
            "elapsed_seconds": None,
        },
    ]
    assert telemetry["knowledge_sources"] == [source.public()]
    assert telemetry["memory"]["status"] == "unavailable"
    assert telemetry["provenance"]["mode"] == "fixture"


def test_recorded_final_evidence_takes_precedence_over_fixture_mode() -> None:
    telemetry = project_telemetry(
        events=[], assets=[], run=SimpleNamespace(mode="fixture", final_metadata={"recorded": True})
    )
    assert telemetry["provenance"]["mode"] == "recorded"


def test_knowledge_provenance_rejects_raw_paths_and_invalid_hashes() -> None:
    event = _event(
        1,
        "ideating",
        "knowledge_context_submitted",
        datetime(2026, 9, 9, tzinfo=UTC),
        {
            "sources": [
                {
                    "source": "/private/provider-payload.txt",
                    "sha256": "a" * 64,
                    "bytes": 3,
                    "kind": "prompt_context",
                },
                {
                    "source": "data/knowledge/ideation/safe.md",
                    "sha256": "not-a-sha",
                    "bytes": 3,
                    "kind": "prompt_context",
                },
            ]
        },
    )
    assert (
        project_telemetry(
            events=[event], assets=[], run=SimpleNamespace(mode="fixture", final_metadata={})
        )["knowledge_sources"]
        == []
    )


def test_creative_progress_exposes_roles_but_not_private_context() -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    events = [
        _event(1, "ideating", "creative_context_prepared", now, {"revision": 2, "count": 12}),
        _event(
            2,
            "ideating",
            "creative_step_started",
            now,
            {
                "role": "explore_object",
                "revision": 2,
                "note": "private feedback",
                "prompt": "private",
            },
        ),
        _event(3, "ideating", "creative_step_completed", now, {"role": ["bad"], "revision": 2}),
    ]
    result = project_telemetry(events=events, assets=[], run=SimpleNamespace(revision=2))
    assert result["events"][1]["detail"] == {"role": "explore_object", "revision": 2}
    assert result["events"][2]["detail"] == {"revision": 2}
    assert result["memory"]["status"] == "scoped"
    assert result["memory"]["concept_count"] == 12
    assert (
        project_telemetry(events=events, assets=[], run=SimpleNamespace(revision=3))["memory"][
            "status"
        ]
        == "unavailable"
    )


def test_public_repair_events_expose_only_bounded_role_attempt_and_counts() -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    rows = public_events(
        [
            _event(
                1,
                "ideating",
                "creative_step_completed",
                now,
                {
                    "role": "repair_1",
                    "source_role": "write_shots",
                    "attempt": 1,
                    "revision": 2,
                    "count": 12,
                    "issues": ["private"],
                    "message": "private",
                    "output": {"raw": "private"},
                    "prompt": "private",
                },
            ),
            _event(
                2,
                "ideating",
                "creative_repair_exhausted",
                now,
                {
                    "role": "repair_2",
                    "attempt": 2,
                    "count": 2,
                    "issues": ["private"],
                    "messages": "private",
                    "output": "private",
                },
            ),
            _event(
                3,
                "ideating",
                "creative_step_started",
                now,
                {
                    "role": "repair_3",
                    "source_role": ["input_moderation"],
                    "attempt": 3,
                    "revision": 2,
                    "raw_issue": "private",
                },
            ),
        ]
    )

    assert [row["detail"] for row in rows] == [
        {
            "role": "repair_1",
            "source_role": "write_shots",
            "attempt": 1,
            "revision": 2,
            "count": 12,
        },
        {"role": "repair_2", "attempt": 2, "count": 2},
        {"revision": 2},
    ]
    assert (
        rows[1]["explanation"]
        == "The allowed corrections did not produce a valid plan. The run stopped."
    )


def test_work_counts_latest_provider_attempts_and_excludes_derived_narration() -> None:
    assets = [
        SimpleNamespace(kind="video", ordinal=n, revision=1, attempt=1, status="ready")
        for n in range(1, 13)
    ]
    assets[0].status = "failed"
    assets.append(SimpleNamespace(kind="video", ordinal=1, revision=1, attempt=2, status="ready"))
    assets.append(
        SimpleNamespace(kind="voice_batch", ordinal=0, revision=1, attempt=1, status="ready")
    )
    assets.extend(
        SimpleNamespace(
            kind="voice",
            ordinal=n,
            revision=1,
            attempt=1,
            status="ready",
            media_metadata={"batch_sha256": "a" * 64},
        )
        for n in range(1, 13)
    )
    work = project_telemetry(events=[], assets=assets, run=SimpleNamespace())["work"]
    assert work["completed_count"] == 13
    assert work["failed_count"] == 0


def test_public_render_progress_and_workflow_phase_remain_bounded() -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    rows = public_events(
        [
            _event(
                1,
                "editing",
                "render_progress",
                now,
                {
                    "revision": 3,
                    "phase": "frames",
                    "progress": 0.4,
                    "total_frames": 202,
                    "rendered_frames": 80,
                    "encoded_frames": 1_000_001,
                    "stitch_stage": "muxing",
                    "job_id": "private",
                    "provider_payload": {"private": True},
                },
            ),
            _event(
                2,
                "editing",
                "workflow_phase",
                now,
                {
                    "revision": 3,
                    "component": "check",
                    "role": "final_media_verification",
                    "state": "running",
                    "counts": {"scenes": 12, "private": 999},
                    "detail": "private",
                },
            ),
            _event(
                3,
                "editing",
                "render_progress",
                now,
                {"revision": 3, "phase": "not-a-phase", "progress": 0.5},
            ),
        ]
    )
    assert [row["detail"] for row in rows] == [
        {
            "revision": 3,
            "phase": "frames",
            "progress": 0.4,
            "total_frames": 202,
            "rendered_frames": 80,
            "stitch_stage": "muxing",
        },
        {
            "revision": 3,
            "component": "check",
            "role": "final_media_verification",
            "state": "running",
            "counts": {"scenes": 12},
        },
        {"revision": 3},
    ]
