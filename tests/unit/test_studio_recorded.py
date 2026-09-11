from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from myloware.providers.media import RecordedNarrationBatchProvider, RecordedVideoProvider
from myloware.studio.editor import approved_recorded_edit_plan
from myloware.studio.recorded import RecordedMediaArchive, RecordedMediaError


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    """Small portable archive contracts; real media proof is a separate local run."""
    from myloware.workflows.monthly import deterministic_fixture_plan

    entries = []

    def entry(role, relative, content, ordinal=None, narration=None):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = content if isinstance(content, bytes) else content.encode()
        path.write_bytes(raw)
        entries.append(
            {
                "role": role,
                "relative_path": relative,
                "sha256": sha256(raw).hexdigest(),
                "byte_size": len(raw),
                "ordinal": ordinal,
                "narration": narration,
            }
        )

    for ordinal in range(1, 13):
        entry(
            "recorded_video_provider_output",
            f"video/{ordinal:02d}-video.mp4",
            f"video-{ordinal}",
            ordinal,
        )
        entry(
            "recorded_voice_provider_output_pause_split",
            f"voice/{ordinal:02d}-voice.wav",
            f"voice-{ordinal}",
            ordinal,
            "January. Teacup.",
        )
    entry("recorded_voice_provider_batch", "voice/original-batch.mp3", "batch")
    entry(
        "recorded_voice_provider_timestamps",
        "voice/timestamps.json",
        json.dumps(
            [
                {
                    "characters": ["a"],
                    "character_start_times_seconds": [0.0],
                    "character_end_times_seconds": [0.1],
                }
            ]
        ),
    )
    entry("recorded_cc0_music", "music/tender-moment.mp3", "music")
    entry(
        "accepted_monthly_plan",
        "plan/dreamlike-plan.json",
        deterministic_fixture_plan(uuid4(), "Teacup").model_dump_json(),
    )
    (tmp_path / "fixture-manifest.json").write_text(json.dumps({"entries": entries}))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/edit-plan.json").write_text(
        json.dumps(
            {
                "version": 1,
                "segment_frames": 202,
                "fps": 30,
                "fade_frames": 30,
                "narration_start_frames": 18,
                "scenes": [
                    {"effects": {"zoomStart": 1.0, "zoomEnd": 1.02, "vignette": 0.06}}
                    for _ in range(12)
                ],
            }
        )
    )
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts/voice-request.json").write_text(json.dumps({"text": "a"}))
    return tmp_path


def test_approved_archive_verifies_every_render_input(archive_root) -> None:
    archive = RecordedMediaArchive(archive_root)
    media = archive.all_media()
    assert len(media) == 26
    assert media[0].ordinal == 1 and media[11].ordinal == 12
    assert media[12].narration == "January. Teacup."
    assert archive.music().role == "recorded_cc0_music"


def test_recorded_plan_is_bound_to_the_visitor_run_without_mutating_archive(archive_root) -> None:
    archive = RecordedMediaArchive(archive_root)
    plan = archive.plan_for(run_id=uuid4(), revision=2, item_text="Teacup")
    assert plan.revision == 2 and plan.item_text == "Teacup" and plan.item_id == "teacup"
    assert len(plan.ideas) == 12


def test_recorded_archive_rejects_tampered_manifest_entry(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    (root / "fixture-manifest.json").write_text("[]", encoding="utf-8")
    with pytest.raises(RecordedMediaError):
        RecordedMediaArchive(root)


def test_approved_edit_recipe_has_one_worker_safe_monthly_controls(archive_root) -> None:
    recipe = approved_recorded_edit_plan(archive_root)
    assert recipe["segment_frames"] == [202] * 12
    assert recipe["fade_frames"] == 30
    assert recipe["narration_start_frames"] == [18] * 12
    assert recipe["narration_volume"] == 0.72
    assert recipe["music_volume"] == 0.23
    assert recipe["music_ducked_volume"] == 0.12


@pytest.mark.asyncio
async def test_recorded_providers_expose_only_signed_internal_relative_paths(archive_root) -> None:
    archive = RecordedMediaArchive(archive_root)
    video = RecordedVideoProvider(base_url="http://127.0.0.1:8311/v1/studio/internal/run/token")
    voice = RecordedNarrationBatchProvider(
        base_url="http://127.0.0.1:8311/v1/studio/internal/run/token",
        timestamps=archive.batch_timestamps(),
    )
    video_submission = await video.submit(prompt="safe", ordinal=1, operation_key="video-1")
    batch_submission = await voice.submit_batch(
        lines=tuple(f"line {index}" for index in range(12)), operation_key="batch-1"
    )
    assert (await video.poll(video_submission.request_id)).url.endswith(
        "/recorded/video/01-video.mp4"
    )
    batch = await voice.poll(batch_submission.request_id)
    assert (
        batch.url and batch.url.endswith("/recorded/voice/original-batch.mp3") and batch.timestamps
    )


def test_recorded_service_composes_without_live_clients(archive_root):
    from myloware.config.studio import StudioSettings
    from myloware.storage.studio_store import StudioStore
    from myloware.studio.moderation import build_moderator
    from myloware.studio.service import StudioService

    service = StudioService(
        StudioStore(
            None, StudioSettings(enabled=True, mode="recorded", recorded_root=archive_root)
        ),
        moderator=build_moderator(mode="fixture", fixture_outcome="allow"),
    )
    assets = service.assets(uuid4())
    assert isinstance(assets.video_provider, RecordedVideoProvider)
    assert isinstance(assets.voice_provider, RecordedNarrationBatchProvider)


def test_declared_asset_hash_is_checked_before_reuse(archive_root):
    archive = RecordedMediaArchive(archive_root)
    (archive_root / "video/01-video.mp4").write_bytes(b"changed source")
    with pytest.raises(RecordedMediaError, match="hash or byte size"):
        archive.video(1)
