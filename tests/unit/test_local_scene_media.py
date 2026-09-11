from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from myloware.providers.media import (
    LOCAL_SCENE_MEDIA_MODEL,
    LocalSceneNarrationBatchProvider,
    LocalSceneVideoProvider,
)
from myloware.studio.local_scene_media import LocalSceneMediaArchive, LocalSceneMediaError


def _entry(root: Path, relative_path: str, ordinal: int, source_title: str) -> dict[str, object]:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"media-{ordinal}-{relative_path}".encode())
    return {
        "ordinal": ordinal,
        "relative_path": relative_path,
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "byte_size": path.stat().st_size,
        "duration_seconds": 1.0,
        "source_title": source_title,
    }


def _archive(root: Path) -> LocalSceneMediaArchive:
    lines = [f"Saved {ordinal} Teacup." for ordinal in range(1, 13)]
    videos = [
        _entry(root, f"video/{ordinal:02d}-video.mp4", ordinal, line.removesuffix("."))
        for ordinal, line in enumerate(lines, 1)
    ]
    titles = [
        _entry(root, f"title/{ordinal:02d}-title.wav", ordinal, line.removesuffix("."))
        for ordinal, line in enumerate(lines, 1)
    ]
    batch = _entry(root, "voice/title-batch.wav", 0, "")
    timestamp = root / "receipts/timestamps.json"
    timestamp.parent.mkdir(parents=True, exist_ok=True)
    timestamp.write_text(
        json.dumps(
            {
                "source_title_text_sha256": sha256("\n\n".join(lines).encode()).hexdigest(),
                "timestamps": [
                    {
                        "characters": [],
                        "character_start_times_seconds": [],
                        "character_end_times_seconds": [],
                    }
                ],
            }
        )
    )
    manifest = {
        "contract_version": "local-scene-media-v1",
        "archive_id": "test-local-replay",
        "private_only": True,
        "source_title_lines": lines,
        "source_title_lines_sha256": sha256("\n".join(lines).encode()).hexdigest(),
        "source_title_text_sha256": sha256("\n\n".join(lines).encode()).hexdigest(),
        "source_archive": {
            "original_batch_sha256": "a" * 64,
            "original_timestamps_sha256": "b" * 64,
            "title_cuts_sha256": "c" * 64,
        },
        "videos": videos,
        "title_clips": titles,
        "title_batch": batch,
        "title_timestamp_receipt": {
            "relative_path": "receipts/timestamps.json",
            "sha256": sha256(timestamp.read_bytes()).hexdigest(),
            "byte_size": timestamp.stat().st_size,
        },
    }
    (root / "local-scene-media-manifest.json").write_text(json.dumps(manifest))
    return LocalSceneMediaArchive(root)


@pytest.mark.asyncio
async def test_local_providers_expose_saved_sources_and_hash_bound_receipt(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    video = LocalSceneVideoProvider(
        archive=archive, trusted_local_base_url="http://127.0.0.1:8452/internal/run/token/local"
    )
    narration = LocalSceneNarrationBatchProvider(
        archive=archive, trusted_local_base_url="http://127.0.0.1:8452/internal/run/token/local"
    )
    video_submission = await video.submit(
        prompt="new requested prompt", ordinal=1, operation_key="video-key"
    )
    batch_submission = await narration.submit_batch(
        lines=tuple(f"New {ordinal}." for ordinal in range(1, 13)), operation_key="batch-key"
    )
    result = await video.poll(video_submission.request_id)
    batch = await narration.poll(batch_submission.request_id)
    assert video_submission.model == batch_submission.model == LOCAL_SCENE_MEDIA_MODEL
    assert result.url.endswith("/video/01-video.mp4")
    assert batch.url.endswith("/voice/title-batch.wav")
    assert batch.input_text == "\n\n".join(archive.source_title_lines())
    assert batch.local_media and batch.local_media["source_batch_sha256"] == archive.batch().sha256
    assert (
        batch.local_media
        and batch.local_media["archive"]["local_batch_sha256"] == archive.batch().sha256
    )


def test_local_archive_rejects_changed_media_before_provider_use(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    archive.video(1).path.write_bytes(b"changed")
    with pytest.raises(LocalSceneMediaError, match="hash mismatch"):
        archive.video(1)
