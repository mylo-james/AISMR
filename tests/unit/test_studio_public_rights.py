from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.config.studio import StudioSettings
from myloware.storage.models import Base
from myloware.storage.studio_models import StudioAsset, StudioRun
from myloware.storage.studio_store import StudioStore
from myloware.studio.public_rights import public_rights_receipt, public_suitability_receipt
from myloware.studio.service import StudioService

FINAL_HASH = "a" * 64
MUSIC_HASH = "b" * 64


def _assets(video_model: str = "video-model", narration_model: str = "narration-model"):
    return [
        *[SimpleNamespace(kind="video", status="ready", model=video_model) for _ in range(12)],
        SimpleNamespace(kind="voice_batch", status="ready", model=narration_model),
    ]


def _profile(*, video_model: str = "video-model", expires_at: str = "2099-01-01T00:00:00Z"):
    return {
        "schema_version": 1,
        "profile_version": "owner-v1",
        "receipt_id": "owner-receipt-123",
        "expires_at": expires_at,
        "provider_policy": {
            "reference": "owner-recorded-provider-policy",
            "permitted_video_model_ids": [video_model],
            "permitted_narration_model_ids": ["narration-model"],
        },
        "music": {
            "id": "tender-moment",
            "sha256": MUSIC_HASH,
            "license_reference": "owner-recorded-music-license",
            "attribution": "Owner-supplied music attribution",
        },
        "labels": {
            "permission_reference": "owner-recorded-label-permission",
            "attribution": "Owner-supplied label attribution",
        },
    }


def _write_profile(tmp_path, profile: dict[str, object]):
    path = tmp_path / "rights-profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    return path


def test_rights_receipt_binds_matching_profile_and_actual_sources_to_final(tmp_path) -> None:
    receipt = public_rights_receipt(
        profile_path=_write_profile(tmp_path, _profile()),
        final_hash=FINAL_HASH,
        assets=_assets(),
        music_id="tender-moment",
        music_sha256=MUSIC_HASH,
        now=datetime(2026, 9, 9, tzinfo=UTC),
    )

    assert receipt["status"] == "passed"
    assert receipt["final_hash"] == FINAL_HASH
    assert receipt["profile_version"] == "owner-v1"
    assert receipt["profile_expires_at"] == "2099-01-01T00:00:00Z"
    assert receipt["source_models"] == {
        "video_model_ids": ["video-model"],
        "narration_model_ids": ["narration-model"],
    }
    assert isinstance(receipt["receipt"], str) and len(receipt["receipt"]) == 64


def test_saved_month_listening_approval_does_not_supply_public_reuse_rights(tmp_path) -> None:
    receipt = public_rights_receipt(
        profile_path=_write_profile(tmp_path, _profile()),
        final_hash=FINAL_HASH,
        assets=_assets(),
        music_id="tender-moment",
        music_sha256=MUSIC_HASH,
        render_provenance={
            "month_bank_sha256": "c" * 64,
            "month_bank_source": {"audio_sha256": "d" * 64},
        },
        now=datetime(2026, 9, 9, tzinfo=UTC),
    )
    assert receipt == {"status": "unverified", "reason": "saved_narration_rights_missing"}


def test_rights_receipt_fails_closed_for_missing_expired_or_mismatched_evidence(tmp_path) -> None:
    missing = public_rights_receipt(
        profile_path=None,
        final_hash=FINAL_HASH,
        assets=_assets(),
        music_id="tender-moment",
        music_sha256=MUSIC_HASH,
    )
    expired = public_rights_receipt(
        profile_path=_write_profile(tmp_path, _profile(expires_at="2000-01-01T00:00:00Z")),
        final_hash=FINAL_HASH,
        assets=_assets(),
        music_id="tender-moment",
        music_sha256=MUSIC_HASH,
        now=datetime(2026, 9, 9, tzinfo=UTC),
    )
    mismatch = public_rights_receipt(
        profile_path=_write_profile(tmp_path, _profile(video_model="another-model")),
        final_hash=FINAL_HASH,
        assets=_assets(),
        music_id="tender-moment",
        music_sha256=MUSIC_HASH,
        now=datetime(2026, 9, 9, tzinfo=UTC),
    )

    assert missing == {"status": "unverified", "reason": "rights_profile_missing"}
    assert expired == {"status": "unverified", "reason": "rights_profile_expired"}
    assert mismatch == {"status": "unverified", "reason": "video_provider_not_permitted"}


def test_suitability_requires_the_actual_passed_moderation_receipt() -> None:
    passed = public_suitability_receipt(
        final_hash=FINAL_HASH,
        moderation={"safe": True, "model": "moderator", "coverage": ["frame-1"]},
    )
    missing = public_suitability_receipt(final_hash=FINAL_HASH, moderation={"safe": False})

    assert passed["status"] == "passed" and passed["final_hash"] == FINAL_HASH
    assert missing == {"status": "unverified", "reason": "final_moderation_receipt_missing"}


@pytest.mark.asyncio
async def test_final_verification_binds_suitability_and_marks_missing_rights_profile(
    tmp_path, monkeypatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rights-service.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = StudioSettings(enabled=True, media_root=tmp_path / "media", active_runs=2)
    store = StudioStore(async_sessionmaker(engine, expire_on_commit=False), config)
    visitor, _ = await store.new_visitor("127.0.0.1")
    run_id = await store.admit(
        visitor_id=visitor.id, item_text="teacup", start_key="rights-service", ip="127.0.0.1"
    )
    try:
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            # This test constructs a historical render receipt without a scene
            # plan or scene-media provenance. Keep that legacy setup explicit.
            run.execution_profile = None
            run.status = "editing"
            run.render_job_id = "render-job"
            # This case exercises the existing v1 final receipt path.
            run.execution_profile = None
            run.render_input_hash = "input-hash"
            run.render_submission_state = "accepted"
            for ordinal in range(1, 13):
                session.add(
                    StudioAsset(
                        run_id=run_id,
                        revision=1,
                        ordinal=ordinal,
                        kind="video",
                        attempt=1,
                        input_hash=f"video-{ordinal}",
                        operation_key=f"rights-video-{ordinal}",
                        provider="video",
                        model="video-model",
                        status="ready",
                    )
                )
            session.add(
                StudioAsset(
                    run_id=run_id,
                    revision=1,
                    ordinal=0,
                    kind="voice_batch",
                    attempt=1,
                    input_hash="voice",
                    operation_key="rights-voice",
                    provider="voice_batch",
                    model="narration-model",
                    status="ready",
                )
            )
        downloaded = config.media_root / str(run_id) / "download.mp4"
        downloaded.parent.mkdir(parents=True)
        downloaded.write_bytes(b"verified-final")

        async def fake_poll(*_args, **_kwargs):
            return {
                "status": "ready",
                "media_verified": True,
                "final_url": "https://renderer/final",
                "render_status": {"phase": "complete", "progress": 1.0},
            }

        async def fake_fetch(**_kwargs):
            return SimpleNamespace(
                path=downloaded,
                duration_seconds=1.0,
                width=1080,
                height=1920,
                byte_size=downloaded.stat().st_size,
                sha256=FINAL_HASH,
                frame_paths=(),
            )

        class PassingModerator:
            async def moderate_images(self, *_args, **_kwargs):
                from dataclasses import dataclass

                @dataclass
                class Verdict:
                    safe: bool = True
                    model: str = "test-moderator"
                    coverage: tuple[str, ...] = ("final-frame",)
                    reason: str = "allowed"

                return Verdict()

        monkeypatch.setattr("myloware.studio.service.MonthlyEditor.poll", fake_poll)
        monkeypatch.setattr("myloware.studio.service.fetch_verified_media", fake_fetch)
        service = StudioService(store, moderator=PassingModerator())
        for _ in range(2):
            await service._record_render_workflow_phase(
                run_id=run_id,
                expected_job_id="render-job",
                expected_input_hash="input-hash",
                role="final_media_verification",
            )
        for status in (
            {"phase": "frames", "progress": 0.11},
            {"phase": "frames", "progress": 0.14},
            {"phase": "frames", "progress": 0.16},
            {"phase": "composition", "progress": 0.2},
        ):
            await service._record_render_progress(
                run_id=run_id,
                expected_job_id="render-job",
                expected_input_hash="input-hash",
                status=status,
            )
        await service.inspect_edit(run_id)

        saved = await store.get_run(run_id)
        metadata = saved.final_metadata
        assert metadata["public_suitability"]["status"] == "passed"
        assert metadata["public_suitability"]["final_hash"] == FINAL_HASH
        assert metadata["public_rights"] == {
            "status": "unverified",
            "reason": "rights_profile_missing",
        }
        snapshot = await store.snapshot(run_id, visitor_id=visitor.id)
        progress = [
            event["detail"] for event in snapshot["events"] if event["type"] == "render_progress"
        ]
        assert progress == [
            {"revision": 1, "phase": "frames", "progress": 0.11},
            {"revision": 1, "phase": "frames", "progress": 0.16},
            {"revision": 1, "phase": "complete", "progress": 1.0},
        ]
        phases = [
            event["detail"]["role"]
            for event in snapshot["events"]
            if event["type"] == "workflow_phase"
        ]
        assert phases == ["final_media_verification", "final_moderation"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_first_running_receipt_leaves_precise_progress_as_the_latest_event(
    tmp_path, monkeypatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'progress-order.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    store = StudioStore(
        async_sessionmaker(engine, expire_on_commit=False),
        StudioSettings(enabled=True, media_root=tmp_path / "media", active_runs=2),
    )
    visitor, _ = await store.new_visitor("127.0.0.1")
    run_id = await store.admit(
        visitor_id=visitor.id, item_text="teacup", start_key="progress-order", ip="127.0.0.1"
    )
    try:
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            assert run is not None
            run.status = "editing"
            run.render_job_id = "render-job"
            run.render_input_hash = "input-hash"
            run.render_submission_state = "accepted"

        async def fake_poll(*_args, **_kwargs):
            return {
                "status": "running",
                "media_verified": False,
                "render_status": {"phase": "frames", "progress": 0.11},
            }

        monkeypatch.setattr("myloware.studio.service.MonthlyEditor.poll", fake_poll)
        service = StudioService(store, moderator=SimpleNamespace())
        await service.inspect_edit(run_id)
        await service.inspect_edit(run_id)

        snapshot = await store.snapshot(run_id, visitor_id=visitor.id)
        render_events = [
            event
            for event in snapshot["events"]
            if event["type"] in {"render_running", "render_progress"}
        ]
        assert [event["type"] for event in render_events] == ["render_running", "render_progress"]
        assert render_events[-1]["detail"] == {
            "revision": 1,
            "phase": "frames",
            "progress": 0.11,
        }
    finally:
        await engine.dispose()
