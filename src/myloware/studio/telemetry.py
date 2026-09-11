"""Public, bounded projections of persisted AISMR studio evidence.

The walkthrough consumes this module's output.  It deliberately projects a
small allow-list instead of serializing storage models or provider payloads.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from hashlib import sha256
from typing import Any

TIMING_DISCLAIMER = (
    "Observed application events only. Durations are wall-clock time between "
    "recorded events, not model reasoning time."
)
MEMORY_UNAVAILABLE = (
    "Application episodic memory is unavailable in this walkthrough. "
    "Coding-agent memory is separate from the application."
)


class LoadedKnowledgeSource:
    """A safe receipt for a document actually read into the ideator context."""

    __slots__ = ("bytes", "kind", "sha256", "source", "text")

    def __init__(self, *, source: str, text: str, kind: str = "source_file_read") -> None:
        self.source = source
        self.text = text
        self.sha256 = sha256(text.encode("utf-8")).hexdigest()
        self.bytes = len(text.encode("utf-8"))
        self.kind = kind

    def public(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "kind": self.kind,
        }


# Every displayed type must be deliberately explained. Unknown event types are
# omitted rather than exposing new producer detail by default.
EVENT_EXPLANATIONS: dict[str, str] = {
    "recorded_plan_loaded": "The saved Teacup plan was loaded. No new ideation occurred.",
    "recorded_assets_loaded": "References to twelve saved scenes and narration sections were loaded.",
    "recorded_assets_matched": "Asset references matched the pinned archive manifest for this approved plan.",
    "recorded_final_selected": "The existing final video was selected. No new render was submitted.",
    "recorded_final_loaded": "The saved final video passed a content hash and byte-size check from storage.",
    "recorded_bundle_changed": "The saved archive changed. This run stopped before reusing it.",
    "recorded_plan_mismatch": "The approved plan did not match the saved final. This run stopped.",
    "recorded_assets_mismatch": "The saved assets did not match the archive. This run stopped.",
    "creative_context_prepared": "Scoped recent concepts and revision feedback were saved for this plan.",
    "creative_step_started": "A planning step was recorded before execution.",
    "creative_step_completed": "A planning step returned a saved result for validation.",
    "creative_step_validated": "A planning step passed the recorded validation checks.",
    "workflow_phase": "A deterministic workflow phase began.",
    "render_progress": "The renderer reported bounded progress for the accepted job.",
    "creative_repair_exhausted": "The allowed corrections did not produce a valid plan. The run stopped.",
    "creative_submission_unknown": "A planning result is uncertain. The request will not be repeated automatically.",
    "admitted": "The run was durably admitted and queued for orchestration.",
    "ideation_submitting": "The bounded ideation request was recorded before execution.",
    "knowledge_loaded": "Approved prompt-context documents were loaded for this ideation request.",
    "knowledge_context_submitted": "The bounded portions of approved documents were included in the ideation request.",
    "visitor_decision": "A visitor review decision was durably recorded.",
    "plan_completed": "The visitor kept the ideas and scripts, completing this planning review.",
    "render_callback_received": "A render status callback was durably received for reconciliation.",
    "video_completed": "The visitor accepted the verified video, completing this run.",
    "library_pruned": "Completed video storage was trimmed to the latest three finals and generated working files were removed.",
    "renderer_copy_removed": "The renderer's duplicate final video was removed after the application saved its verified copy.",
    "renderer_cleanup_deferred": "The renderer could not identify the old job for immediate cleanup; its output retention fallback still applies.",
    "runtime_mode_mismatch": "The run stopped because its durable mode did not match the active runtime configuration.",
    "ideation_failed": "Ideation did not produce a usable plan.",
    "asset_submission_failed": "A media job submission did not produce an accepted receipt.",
    "asset_gather_failed": "A generated media asset did not pass the required gathering or verification checks.",
    "render_failed": "The render did not produce a verified final video.",
    "input_moderation_checked": "The visitor item passed the recorded input safety check.",
    "plan_moderation_checked": "The generated plan passed the recorded plan safety check.",
    "plan_ready": "A twelve-scene plan was durably saved for visitor review.",
    "ideas_approved": "Visitor approval unlocked the production jobs.",
    "asset_intents_created": "Durable media job intents were created before provider submission.",
    "assets_submitting": "Media jobs were handed off for parallel provider submission.",
    "assets_gathering": "Completed media jobs are being gathered and verified in order.",
    "asset_moderation_checked": "Generated media passed the recorded safety check.",
    "assets_ready": "All required media assets were durably gathered and verified.",
    "render_submitting": "The ordered edit was durably recorded before render submission.",
    "render_queued": "The render job was accepted and is waiting for execution.",
    "render_running": "The render job is running.",
    "final_moderation_checked": "The final video passed the recorded safety check.",
    "render_verified": "The final playable video was hash-checked and durably recorded.",
    "input_moderation_blocked": "The run stopped because input safety did not allow it.",
    "plan_moderation_blocked": "The run stopped because plan safety did not allow it.",
    "final_moderation_blocked": "The run stopped because final-media safety did not allow it.",
    "asset_retries_exhausted": "The run stopped after its bounded asset retry budget was exhausted.",
    "submission_unknown": "A provider receipt is ambiguous, so orchestration paused instead of resubmitting blindly.",
    "ideation_submission_unknown": "The ideation receipt is ambiguous, so orchestration paused instead of repeating it.",
    "render_unavailable": "The render submission did not produce an accepted receipt.",
}

_COMPONENTS = frozenset({"agent", "worker", "check"})
_WORKFLOW_ROLES = frozenset(
    {
        "prepare_context",
        "explore_object",
        "explore_surreal",
        "curate",
        "write_shots",
        "replenish",
        "recurate",
        "input_moderation",
        "plan_moderation",
        "repair_1",
        "repair_2",
        "media_submission",
        "media_gathering",
        "narration_assembly",
        "render_assembly",
        "renderer",
        "final_media_verification",
        "final_moderation",
        "public_eligibility",
    }
)
_WORKFLOW_STATES = frozenset(
    {"queued", "submitted", "running", "received", "validated", "blocked", "failed", "unknown"}
)
_RENDER_PHASES = frozenset(
    {
        "queued",
        "preparing",
        "composition",
        "frames",
        "encoding",
        "verification",
        "complete",
        "failed",
    }
)
_STITCH_STAGES = frozenset({"encoding", "muxing"})


_DETAIL_KEYS = frozenset(
    {
        "count",
        "revision",
        "months",
        "scenes",
        "duration_seconds",
        "width",
        "height",
        "kept",
        "evicted",
        "removed_entries",
        "removed_bytes",
    }
)


def public_events(events: Iterable[object]) -> list[dict[str, Any]]:
    """Return only documented event fields and harmless, scalar detail keys."""
    result: list[dict[str, Any]] = []
    for event in events:
        event_type = str(_field(event, "event_type", "type", default=""))
        explanation = EVENT_EXPLANATIONS.get(event_type)
        if explanation is None:
            continue
        detail = _public_detail(_field(event, "detail", default={}))
        stage = str(_field(event, "stage", default=""))
        result.append(
            {
                "sequence": int(_field(event, "sequence", default=0)),
                "occurred_at": _timestamp(_field(event, "created_at", "occurred_at")),
                "type": event_type,
                "explanation": explanation,
                "subject": stage or None,
                "detail": detail,
            }
        )
    return result


def project_telemetry(
    *, events: Iterable[object], assets: Iterable[object], run: object
) -> dict[str, Any]:
    """Project observed durable state into the stable walkthrough contract."""
    event_rows = list(events)
    public = public_events(event_rows)
    return {
        "kind": "observed",
        "disclaimer": TIMING_DISCLAIMER,
        "stage_timings": _stage_timings(event_rows),
        "work": _work_counts(assets),
        "events": public,
        "knowledge_sources": _knowledge_sources(event_rows),
        "memory": _planning_memory(event_rows, run),
        "provenance": _provenance(run),
    }


def _stage_timings(events: list[object]) -> list[dict[str, Any]]:
    rows = sorted(events, key=lambda row: int(_field(row, "sequence", default=0)))
    groups: list[list[object]] = []
    for row in rows:
        stage = str(_field(row, "stage", default=""))
        if not stage:
            continue
        if not groups or str(_field(groups[-1][0], "stage", default="")) != stage:
            groups.append([row])
        else:
            groups[-1].append(row)
    output: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        started = _as_datetime(_field(group[0], "created_at", "occurred_at"))
        next_group = groups[index + 1] if index + 1 < len(groups) else None
        ended = (
            _as_datetime(_field(next_group[0], "created_at", "occurred_at")) if next_group else None
        )
        elapsed = (ended - started).total_seconds() if started and ended else None
        output.append(
            {
                "stage": str(_field(group[0], "stage")),
                "started_at": _timestamp(started),
                "ended_at": _timestamp(ended),
                "elapsed_seconds": round(max(0.0, elapsed), 3) if elapsed is not None else None,
            }
        )
    return output


def _work_counts(assets: Iterable[object]) -> dict[str, int]:
    latest: dict[tuple[Any, ...], object] = {}
    for index, asset in enumerate(assets):
        kind = _field(asset, "kind")
        metadata = _field(asset, "media_metadata", default={})
        if kind == "voice" and isinstance(metadata, Mapping) and metadata.get("batch_sha256"):
            continue  # Locally split narration is an output, not another provider job.
        key = (_field(asset, "revision"), _field(asset, "ordinal"), kind)
        if kind is None:
            key = (index,)  # Legacy callers without scene identity cannot be deduplicated.
        previous = latest.get(key)
        if previous is None or int(_field(asset, "attempt", default=1)) >= int(
            _field(previous, "attempt", default=1)
        ):
            latest[key] = asset
    statuses = Counter(str(_field(asset, "status", default="")) for asset in latest.values())
    return {
        "queued_count": statuses["queued"],
        "submitting_count": statuses["submitting"],
        "running_count": statuses["running"],
        "completed_count": statuses["ready"] + statuses["gathered"],
        "failed_count": sum(
            statuses[state] for state in ("failed", "unknown", "submission_unknown")
        ),
    }


def _planning_memory(events: list[object], run: object) -> dict[str, Any]:
    for event in reversed(events):
        detail = _field(event, "detail", default={})
        if (
            _field(event, "event_type", "type") == "creative_context_prepared"
            and isinstance(detail, Mapping)
            and detail.get("revision") == _field(run, "revision")
        ):
            return {
                "status": "scoped",
                "concept_count": detail.get("count", 0),
                "explanation": "Recent concepts belong to this visitor and expire with the planning retention period.",
            }
    return {"status": "unavailable", "explanation": MEMORY_UNAVAILABLE}


def _knowledge_sources(events: Iterable[object]) -> list[dict[str, Any]]:
    fallback: list[dict[str, Any]] = []
    for event in events:
        event_type = _field(event, "event_type", "type", default="")
        if event_type not in {"knowledge_loaded", "knowledge_context_submitted"}:
            continue
        sources = _field(event, "detail", default={})
        if not isinstance(sources, Mapping):
            continue
        rows = sources.get("sources")
        if not isinstance(rows, list):
            continue
        safe: list[dict[str, Any]] = []
        for row in rows[:3]:
            if not isinstance(row, Mapping):
                continue
            source, digest, size, kind = (
                row.get("source"),
                row.get("sha256"),
                row.get("bytes"),
                row.get("kind"),
            )
            if (
                isinstance(source, str)
                and source.startswith("data/")
                and ".." not in source.split("/")
                and isinstance(digest, str)
                and len(digest) == 64
                and all(character in "0123456789abcdef" for character in digest)
                and isinstance(size, int)
                and size >= 0
                and kind in {"source_file_read", "prompt_context"}
            ):
                safe.append({"source": source, "sha256": digest, "bytes": size, "kind": kind})
        if event_type == "knowledge_context_submitted":
            return safe
        fallback = safe
    return fallback


def _provenance(run: object) -> dict[str, str]:
    metadata = _field(run, "final_metadata", default={})
    recorded = isinstance(metadata, Mapping) and metadata.get("recorded") is True
    mode = "recorded" if recorded else str(_field(run, "mode", default="unknown"))
    explanations = {
        "fixture": "This walkthrough used deterministic fixture providers; it is not a live provider run.",
        "recorded": "This walkthrough uses recorded media evidence; it is not a new live generation.",
        "live": "This walkthrough records observed live-provider receipts and durable state.",
    }
    return {
        "mode": mode,
        "explanation": explanations.get(
            mode, "The execution source is not established by the available durable state."
        ),
    }


def _public_detail(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    component = value.get("component")
    if isinstance(component, str) and component in _COMPONENTS:
        result["component"] = component
    role = value.get("role")
    if isinstance(role, str) and role in _WORKFLOW_ROLES:
        result["role"] = role
    source_role = value.get("source_role")
    if isinstance(source_role, str) and source_role in {
        "explore_object",
        "explore_surreal",
        "curate",
        "write_shots",
        "replenish",
        "recurate",
    }:
        result["source_role"] = source_role
    attempt = value.get("attempt")
    if type(attempt) is int and 1 <= attempt <= 2:
        result["attempt"] = attempt
    state = value.get("state")
    if isinstance(state, str) and state in _WORKFLOW_STATES:
        result["state"] = state
    counts = value.get("counts")
    if isinstance(counts, Mapping):
        public_counts = {
            key: count
            for key in ("videos", "narration_batches", "scenes")
            if type(count := counts.get(key)) is int and 0 <= count <= 1_000_000
        }
        if public_counts:
            result["counts"] = public_counts
    phase = value.get("phase")
    progress = value.get("progress")
    if (
        isinstance(phase, str)
        and phase in _RENDER_PHASES
        and not isinstance(progress, bool)
        and isinstance(progress, (int, float))
        and 0.0 <= float(progress) <= 1.0
    ):
        result["phase"] = phase
        result["progress"] = float(progress)
        for key in ("total_frames", "rendered_frames", "encoded_frames"):
            counter = value.get(key)
            if type(counter) is int and 0 <= counter <= 1_000_000:
                result[key] = counter
        stitch_stage = value.get("stitch_stage")
        if isinstance(stitch_stage, str) and stitch_stage in _STITCH_STAGES:
            result["stitch_stage"] = stitch_stage
    for key in _DETAIL_KEYS:
        # Early cleanup receipts counted directories as well as files. Keep
        # their evidence readable under the accurate public label.
        candidate = value.get(key, value.get("removed_files") if key == "removed_entries" else None)
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, (int, float)):
            result[key] = candidate
    return result


def _field(value: object, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.removesuffix("Z"))
        except ValueError:
            return None
    return None


def _timestamp(value: object) -> str | None:
    parsed = _as_datetime(value)
    return parsed.isoformat() + "Z" if parsed else None
