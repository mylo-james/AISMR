from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import myloware.studio.assets as asset_module
from myloware.config.studio import StudioSettings
from myloware.providers.media import AssetResult
from myloware.storage.models import Base, Job, _utc_now
from myloware.storage.studio_models import (
    StudioAsset,
    StudioDecision,
    StudioPlannerRun,
    StudioPlanningOperation,
    StudioRun,
)
from myloware.storage.studio_store import StudioStore
from myloware.studio.assets import MonthlyAssets
from myloware.studio.execution_profile import build_execution_profile
from myloware.studio.housekeeping import run_studio_housekeeping
from myloware.studio.library_service import StudioLibraryService
from myloware.studio.media import MediaFetchPolicy, VerifiedMedia
from myloware.studio.moderation import FixtureModerator
from myloware.studio.voice_batch import build_scene_narration_text
from myloware.workflows.scenes import deterministic_scene_plan


class _NoSubmitProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def submit(self, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("gathering a saved receipt must not submit media")

    async def submit_batch(self, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("gathering a saved receipt must not resubmit narration")


def _settings(tmp_path: Path) -> StudioSettings:
    return StudioSettings(
        enabled=True,
        mode="live",
        live_enabled=True,
        preserve_run_artifacts=True,
        FAL_API_KEY="fal",
        OPENAI_API_KEY="openai",
        session_secret="s" * 32,
        daily_budget_usd=100,
        run_reservation_usd=100,
        render_real=True,
        cost_profile_version="private-evidence-v1",
        cost_input_moderation_usd="0.01",
        cost_ideation_usd="0.01",
        cost_plan_moderation_usd="0.01",
        cost_video_request_usd="0.01",
        cost_narration_batch_usd="0.01",
        cost_render_usd="0.01",
        cost_final_moderation_usd="0.01",
        media_root=tmp_path / "media",
        fixture_root=tmp_path / "fixtures",
    )


async def _store(tmp_path: Path) -> tuple[StudioStore, object]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'private-live.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return (
        StudioStore(async_sessionmaker(engine, expire_on_commit=False), _settings(tmp_path)),
        engine,
    )


async def _admit(store: StudioStore, offset: int) -> tuple[object, object]:
    visitor, _ = await store.new_visitor(f"127.0.0.{offset + 1}")
    run_id = await store.admit(
        visitor_id=visitor.id,
        item_text="teacup",
        start_key=f"private-evidence-{offset:08d}",
        ip="127.0.0.1",
    )
    return visitor, run_id


async def _final_review(store: StudioStore, run_id: object) -> None:
    run_dir = store.config.media_root / str(run_id)
    run_dir.mkdir(parents=True)
    (run_dir / "raw-video.media").write_bytes(b"raw-video")
    (run_dir / "raw-batch.media").write_bytes(b"raw-batch")
    (run_dir / "01-voice.wav").write_bytes(b"split-voice")
    (run_dir / ("a" * 64 + "-splits")).mkdir()
    (run_dir / ("a" * 64 + "-splits") / "02-voice.wav").write_bytes(b"split-two")
    final = run_dir / "final.mp4"
    final.write_bytes(b"final-video")
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        assert run is not None
        run.status = "final_review"
        run.final_hash = sha256(final.read_bytes()).hexdigest()
        run.final_metadata = {"media_verified": True, "path": str(final.resolve())}


@pytest.mark.asyncio
async def test_private_live_acceptance_keeps_all_operational_media(tmp_path: Path) -> None:
    store, engine = await _store(tmp_path)
    try:
        visitor, run_id = await _admit(store, 1)
        await _final_review(store, run_id)
        run = await store.get_run(run_id)
        await store.decide(
            run_id=run_id,
            visitor_id=visitor.id,
            gate="final",
            revision=1,
            subject_hash=store.final_review_hash(run),
            decision="approve",
            decision_key="preserve-final-acceptance",
        )
        async with store.factory() as session:
            jobs = (await session.scalars(select(Job).where(Job.run_id == run_id))).all()
        assert any(job.job_type == "studio.cleanup" for job in jobs)

        service = StudioLibraryService(store)

        def no_filesystem_cleanup(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("private evidence retention must not apply cleanup")

        async def no_renderer_cleanup(_run: StudioRun) -> None:
            raise AssertionError("private evidence retention must not delete renderer output")

        service.files.apply = no_filesystem_cleanup  # type: ignore[method-assign]
        service._renderer_copy = no_renderer_cleanup  # type: ignore[method-assign]
        await service.cleanup(run_id)
        run_dir = store.config.media_root / str(run_id)
        assert {path.name for path in run_dir.iterdir()} >= {
            "raw-video.media",
            "raw-batch.media",
            "01-voice.wav",
            "final.mp4",
        }
        async with store.factory() as session:
            saved = await session.get(StudioRun, run_id)
        assert saved is not None and "retention_cleanup" not in (saved.final_metadata or {})
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_private_live_housekeeping_keeps_expired_media_and_raw_planner_receipts(
    tmp_path: Path,
) -> None:
    store, engine = await _store(tmp_path)
    try:
        _visitor, run_id = await _admit(store, 2)
        await _final_review(store, run_id)
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            profile = await session.get(StudioPlannerRun, run_id)
            assert run is not None and profile is not None
            run.final_review_expires_at = _utc_now() - timedelta(seconds=1)
            profile.expires_at = _utc_now() - timedelta(seconds=1)
            session.add(
                StudioPlanningOperation(
                    run_id=run_id,
                    revision=1,
                    role="write_shots",
                    input_hash="a" * 64,
                    request={"full": "planner request"},
                    response={"value": {"shots": ["planner response"]}},
                    status="ready",
                    deadline_at=_utc_now(),
                    expires_at=_utc_now() - timedelta(seconds=1),
                )
            )
        receipt = await run_studio_housekeeping(store)
        assert receipt.cleanup_queued == (run_id,)
        await StudioLibraryService(store).cleanup(run_id)
        assert (store.config.media_root / str(run_id) / "raw-video.media").is_file()
        async with store.factory() as session:
            operation = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == run_id,
                    StudioPlanningOperation.role == "write_shots",
                )
            )
        assert operation is not None
        assert operation.request == {"full": "planner request"}
        assert operation.response == {"value": {"shots": ["planner response"]}}
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_private_live_keeps_audio_and_raw_timestamps_when_alignment_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, engine = await _store(tmp_path)
    try:
        visitor, run_id = await _admit(store, 3)
        plan = deterministic_scene_plan(run_id, "teacup")
        profile = build_execution_profile()
        lines = tuple(f"{scene.title}." for scene in plan.scenes)
        submitted_text = build_scene_narration_text(lines)
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            assert run is not None
            run.status = "generating"
            run.plan = plan.model_dump(mode="json")
            run.plan_hash = plan.canonical_sha256
            run.plan_approved_hash = plan.canonical_sha256
            run.execution_profile = profile
            session.add(
                StudioDecision(
                    run_id=run_id,
                    visitor_id=visitor.id,
                    gate="ideas",
                    revision=1,
                    subject_hash=plan.canonical_sha256,
                    request_key="preserve-invalid-timestamps",
                    decision="approve",
                    payload={},
                    expires_at=_utc_now() + timedelta(hours=1),
                )
            )
            batch = StudioAsset(
                run_id=run_id,
                revision=1,
                ordinal=0,
                kind="voice_batch",
                attempt=1,
                input_hash=sha256("\n".join(lines).encode()).hexdigest(),
                operation_key=f"{run_id}:1:0:voice_batch:1",
                provider="fal",
                model=profile["voice_profile"]["model"],
                request_id="saved-batch-receipt",
                status="queued",
                media_metadata={
                    "voice_profile": profile["voice_profile"],
                    "voice_profile_sha256": profile["voice_profile_sha256"],
                    "submitted_text": submitted_text,
                    "submitted_text_sha256": sha256(submitted_text.encode()).hexdigest(),
                },
            )
            session.add(batch)
            await session.flush()
            batch_id = batch.id

        preserved_audio = store.config.media_root / str(run_id) / ("b" * 64 + ".media")

        async def fake_fetch(**_kwargs: object) -> VerifiedMedia:
            preserved_audio.parent.mkdir(parents=True, exist_ok=True)
            preserved_audio.write_bytes(b"original-batch-audio")
            return VerifiedMedia(
                path=preserved_audio,
                sha256="b" * 64,
                byte_size=20,
                duration_seconds=2.0,
                width=0,
                height=0,
                frame_paths=(),
                metadata={},
            )

        monkeypatch.setattr(asset_module, "fetch_verified_media", fake_fetch)
        video, voice = _NoSubmitProvider(), _NoSubmitProvider()
        assets = MonthlyAssets(
            store,
            video,
            voice,
            FixtureModerator("allow"),
            MediaFetchPolicy(frozenset({"http://127.0.0.1"}), 1024, 4096, fixture_mode=True),
        )
        invalid_timestamps = (
            {
                "characters": ["wrong"],
                "character_start_times_seconds": [0.0],
                "character_end_times_seconds": [0.1],
            },
        )
        await assets._gather_batch(
            batch_id,
            AssetResult(
                "ready",
                url="http://127.0.0.1/title-batch.wav",
                timestamps=invalid_timestamps,
            ),
        )
        await assets._retry_definitive_failures(run_id)

        assert preserved_audio.read_bytes() == b"original-batch-audio"
        assert video.calls == voice.calls == 0
        async with store.factory() as session:
            saved = await session.get(StudioAsset, batch_id)
            retries = (
                await session.scalars(
                    select(StudioAsset).where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.kind == "voice_batch",
                        StudioAsset.attempt > 1,
                    )
                )
            ).all()
        assert saved is not None
        assert saved.status == "failed" and saved.error_code == "voice_batch_validation_failed"
        assert saved.media_metadata["provider_timestamps"] == list(invalid_timestamps)
        assert saved.media_metadata["timestamps_verified"] is False
        assert saved.media_metadata["received_audio_sha256"] == "b" * 64
        assert retries == []
    finally:
        await engine.dispose()  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("mode", "origin", "cookie_secure", "public_runtime_enabled"),
    [
        ("fixture", "http://127.0.0.1:8311", False, False),
        ("local", "http://127.0.0.1:8311", False, False),
        ("live", "https://studio.example.test", True, False),
        ("live", "http://127.0.0.1:8311", False, True),
    ],
)
def test_artifact_preservation_is_rejected_outside_private_live(
    mode: str, origin: str, cookie_secure: bool, public_runtime_enabled: bool
) -> None:
    with pytest.raises(ValueError, match="Preserving run artifacts requires private live mode"):
        StudioSettings(
            enabled=True,
            mode=mode,
            origin=origin,
            cookie_secure=cookie_secure,
            public_runtime_enabled=public_runtime_enabled,
            preserve_run_artifacts=True,
        )
