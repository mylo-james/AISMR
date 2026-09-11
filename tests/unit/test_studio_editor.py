from __future__ import annotations

from typing import Self
from uuid import uuid4

import httpx
import pytest

from myloware.studio.editor import MonthlyEditor, SceneOutput
from myloware.workflows.monthly import deterministic_fixture_plan


@pytest.mark.asyncio
async def test_fake_editor_never_claims_success() -> None:
    result = await MonthlyEditor(real_render=False).submit(
        run_id="r",
        plan=deterministic_fixture_plan(uuid4(), "chair"),
        video_urls=[],
        narration_urls=[],
        input_hash="x",
    )
    assert result == {"status": "fake_unavailable", "media_verified": False}


class _Client:
    def __init__(self, response: httpx.Response, seen: dict[str, object]) -> None:
        self.response, self.seen = response, seen

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.seen.update({"url": url, **kwargs})
        self.response.request = httpx.Request("POST", url)
        return self.response

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.seen.update({"url": url, **kwargs})
        self.response.request = httpx.Request("GET", url)
        return self.response


def _editor(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response, seen: dict[str, object]
) -> MonthlyEditor:
    monkeypatch.setattr("myloware.studio.editor.settings.remotion_api_secret", "secret")
    monkeypatch.setattr(
        "myloware.studio.editor.httpx.AsyncClient",
        lambda **_kwargs: _Client(response, seen),
    )
    return MonthlyEditor()


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_status", ["queued", "rendering", "done"])
async def test_submit_sends_exact_monthly_payload_and_callback(
    monkeypatch: pytest.MonkeyPatch,
    ack_status: str,
) -> None:
    seen: dict[str, object] = {}
    editor = _editor(
        monkeypatch,
        httpx.Response(202, json={"job_id": "job-1", "status": ack_status, "input_hash": "hash"}),
        seen,
    )
    result = await editor.submit(
        run_id="run",
        plan=deterministic_fixture_plan(uuid4(), "chair"),
        video_urls=["https://media.example/video.mp4"] * 12,
        narration_urls=["https://media.example/voice.wav"] * 12,
        music_url="https://media.example/music.wav",
        input_hash="hash",
        callback_url="https://app.example/webhooks/remotion",
    )
    assert result == {
        "status": "accepted",
        "render_job_id": "job-1",
        "media_verified": False,
    }
    payload = seen["json"]
    assert isinstance(payload, dict)
    assert set(payload) == {
        "run_id",
        "input_hash",
        "template",
        "clips",
        "objects",
        "narration_urls",
        "fps",
        "width",
        "height",
        "music_url",
        "callback_url",
    }
    assert (
        payload["template"] == "monthly"
        and len(payload["clips"]) == len(payload["objects"]) == len(payload["narration_urls"]) == 12
    )
    assert payload["callback_url"] == "https://app.example/webhooks/remotion"
    assert payload["input_hash"] == "hash"


@pytest.mark.asyncio
async def test_editor_rejects_http_and_malformed_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        await _editor(monkeypatch, httpx.Response(400), {}).submit(
            run_id="run",
            plan=deterministic_fixture_plan(uuid4(), "chair"),
            video_urls=["https://m.example/a"] * 12,
            narration_urls=["https://m.example/a"] * 12,
            input_hash="h",
        )
    )["status"] == "rejected"
    assert (
        await _editor(monkeypatch, httpx.Response(202, content=b"not-json"), {}).submit(
            run_id="run",
            plan=deterministic_fixture_plan(uuid4(), "chair"),
            video_urls=["https://m.example/a"] * 12,
            narration_urls=["https://m.example/a"] * 12,
            input_hash="h",
        )
    )["status"] == "unknown"


@pytest.mark.asyncio
async def test_poll_binds_run_and_exposes_only_verified_final_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    unverified = await _editor(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "run_id": "run",
                "input_hash": "hash",
                "status": "done",
                "output_url": "https://media.example/final.mp4",
                "media_diagnostics": {"verified": False},
            },
        ),
        seen,
    ).poll(run_id="run", render_job_id="job", input_hash="hash")
    assert unverified["status"] == "ready" and "final_url" not in unverified
    mismatched = await _editor(
        monkeypatch,
        httpx.Response(200, json={"run_id": "other", "input_hash": "hash", "status": "done"}),
        {},
    ).poll(run_id="run", render_job_id="job", input_hash="hash")
    assert mismatched == {"status": "unknown", "media_verified": False}
    verified = await _editor(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "run_id": "run",
                "input_hash": "hash",
                "status": "done",
                "output_url": "https://media.example/final.mp4",
                "media_diagnostics": {"verified": True},
            },
        ),
        {},
    ).poll(run_id="run", render_job_id="job", input_hash="hash")
    assert verified["final_url"] == "https://media.example/final.mp4"
    mismatched_hash = await _editor(
        monkeypatch,
        httpx.Response(200, json={"run_id": "run", "input_hash": "other", "status": "done"}),
        {},
    ).poll(run_id="run", render_job_id="job", input_hash="hash")
    assert mismatched_hash == {"status": "unknown", "media_verified": False}


@pytest.mark.asyncio
async def test_poll_keeps_queued_distinct_from_running(monkeypatch: pytest.MonkeyPatch) -> None:
    queued = await _editor(
        monkeypatch,
        httpx.Response(200, json={"run_id": "run", "input_hash": "hash", "status": "queued"}),
        {},
    ).poll(run_id="run", render_job_id="job", input_hash="hash")
    assert queued["status"] == "queued"


@pytest.mark.asyncio
async def test_editor_forwards_optional_monthly_edit_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    editor = _editor(
        monkeypatch,
        httpx.Response(202, json={"job_id": "job-1", "status": "queued", "input_hash": "hash"}),
        seen,
    )
    plan = {
        "segment_frames": [202] * 12,
        "clip_playback_rates": [1.0] * 12,
        "fade_frames": 30,
        "narration_start_frames": [18] * 12,
        "scene_effects": [{"zoomStart": 1.03, "zoomEnd": 1.09, "vignette": 0.1}] * 12,
    }
    result = await editor.submit(
        run_id="run",
        plan=deterministic_fixture_plan(uuid4(), "chair"),
        video_urls=["https://media.example/video.mp4"] * 12,
        narration_urls=["https://media.example/voice.wav"] * 12,
        input_hash="hash",
        edit_plan=plan,
    )
    assert result["status"] == "accepted"
    assert isinstance(seen["json"], dict) and seen["json"]["edit_plan"] == plan


def _scene_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "pipeline_version": "scene-v2",
        "voice_profile": {"profile_version": "v1"},
        "voice_profile_sha256": "a" * 64,
        "render_preset_id": "monthly-saved-voice-v2",
        "render_preset_sha256": "b" * 64,
    }


def _scene_provenance(*, archive_id: str | None = None) -> dict[str, object]:
    provenance: dict[str, object] = {
        "render_contract": "scene-v2",
        "render_preset_id": "monthly-saved-voice-v2",
        "render_preset_sha256": "b" * 64,
        "month_bank_id": "teacup-whisper-months-v1",
        "month_bank_sha256": "c" * 64,
        "voice_profile_sha256": "a" * 64,
        "voice_profile": {"profile_version": "v1"},
        "assembled_wav_sha256": ["d" * 64] * 12,
        "title_audio_sha256": ["e" * 64] * 12,
        "video_sha256": ["f" * 64] * 12,
        "bank_clip_refs": [
            {
                "ordinal": ordinal,
                "label": f"month-{ordinal}",
                "filename": f"{ordinal}.wav",
                "sha256": "1" * 64,
            }
            for ordinal in range(1, 13)
        ],
    }
    if archive_id is not None:
        provenance["narration_archive"] = {
            "schema_version": 1,
            "archive_id": archive_id,
            "manifest_sha256": "2" * 64,
            "file_count": 12,
            "untrusted_extra": "discarded",
        }
    return provenance


@pytest.mark.asyncio
async def test_poll_retains_only_archive_receipt_bound_to_current_render_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = await _editor(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "run_id": "run",
                "input_hash": "hash",
                "status": "done",
                "output_url": "https://media.example/final.mp4",
                "media_diagnostics": {
                    "verified": True,
                    "provenance": _scene_provenance(archive_id="job"),
                },
            },
        ),
        {},
    ).poll(run_id="run", render_job_id="job", input_hash="hash")
    assert valid["render_provenance"]["narration_archive"] == {
        "schema_version": 1,
        "archive_id": "job",
        "manifest_sha256": "2" * 64,
        "file_count": 12,
    }

    mismatched = await _editor(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "run_id": "run",
                "input_hash": "hash",
                "status": "done",
                "media_diagnostics": {
                    "verified": True,
                    "provenance": _scene_provenance(archive_id="other-job"),
                },
            },
        ),
        {},
    ).poll(run_id="run", render_job_id="job", input_hash="hash")
    assert "render_provenance" not in mismatched


@pytest.mark.asyncio
async def test_submit_scenes_hides_calendar_and_binds_the_pinned_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    editor = _editor(
        monkeypatch,
        httpx.Response(202, json={"job_id": "job-v2", "status": "queued", "input_hash": "hash"}),
        seen,
    )
    scenes = [
        SceneOutput(
            ordinal=index,
            title=f"Scene {index}",
            video_ref=f"https://media.example/{index}.mp4",
            title_audio_ref=f"https://media.example/{index}.wav",
        )
        for index in range(1, 13)
    ]
    assert (
        await editor.submit_scenes(
            run_id="run", scenes=scenes, execution_profile=_scene_profile(), input_hash="hash"
        )
    )["status"] == "accepted"
    payload = seen["json"]
    assert isinstance(payload, dict)
    assert payload["template"] == "monthly-scene-v2"
    assert payload["render_preset"] == {"id": "monthly-saved-voice-v2", "sha256": "b" * 64}
    assert "January" not in str(payload)
    assert payload["ordered_scenes"][0]["ordinal"] == 1


@pytest.mark.asyncio
async def test_preflight_scenes_uses_the_authenticated_renderer_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    editor = _editor(monkeypatch, httpx.Response(200, json={"status": "ready"}), seen)
    await editor.preflight_scenes(_scene_profile())
    assert seen["url"] == "http://localhost:3001/api/render/presets/preflight"
    assert seen["headers"] == {"Authorization": "Bearer secret", "x-api-key": "secret"}


@pytest.mark.asyncio
async def test_poll_projects_only_valid_bounded_render_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = await _editor(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "run_id": "run",
                "input_hash": "hash",
                "status": "rendering",
                "render_status": {
                    "schema_version": 1,
                    "phase": "frames",
                    "progress": 0.37,
                    "total_frames": 202,
                    "rendered_frames": 75,
                    "encoded_frames": -1,
                    "stitch_stage": "muxing",
                    "internal_trace": "private",
                },
            },
        ),
        {},
    ).poll(run_id="run", render_job_id="job", input_hash="hash")
    assert valid["render_status"] == {
        "phase": "frames",
        "progress": 0.37,
        "total_frames": 202,
        "rendered_frames": 75,
        "stitch_stage": "muxing",
    }

    malformed = await _editor(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "run_id": "run",
                "input_hash": "hash",
                "status": "done",
                "output_url": "https://media.example/final.mp4",
                "media_diagnostics": {"verified": True},
                "render_status": {"schema_version": True, "phase": "frames", "progress": 0.5},
            },
        ),
        {},
    ).poll(run_id="run", render_job_id="job", input_hash="hash")
    assert malformed["status"] == "ready"
    assert malformed["final_url"] == "https://media.example/final.mp4"
    assert "render_status" not in malformed
