from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import myloware.studio.assets as asset_module
from myloware.config.studio import StudioSettings
from myloware.providers.media import (
    FAKE_NARRATION_BATCH_MODEL,
    LOCAL_SCENE_MEDIA_MODEL,
    AssetResult,
    Submission,
    SubmissionUnknown,
)
from myloware.storage.models import Base, _utc_now
from myloware.storage.studio_models import (
    StudioAsset,
    StudioCostLedger,
    StudioDecision,
    StudioEvent,
    StudioReservation,
    StudioRun,
)
from myloware.storage.studio_store import StudioStore
from myloware.studio.assets import MonthlyAssets
from myloware.studio.execution_profile import build_execution_profile
from myloware.studio.media import MediaFetchPolicy, MediaVerificationError, VerifiedMedia
from myloware.studio.moderation import FixtureModerator
from myloware.studio.scene_media import validate_scene_assets
from myloware.studio.voice_batch import VoiceSplit, build_scene_narration_text
from myloware.workflows.monthly import deterministic_fixture_plan
from myloware.workflows.scenes import deterministic_scene_plan


class Provider:
    def __init__(self, kind: str, *, unknown: bool = False) -> None:
        self.kind, self.unknown, self.calls, self.release = (
            kind,
            unknown,
            [],
            asyncio.Event(),
        )
        self.batch_calls: list[tuple[str, ...]] = []
        self.batch_profiles: list[dict[str, object] | None] = []

    async def submit_batch(
        self,
        *,
        lines: tuple[str, ...],
        operation_key: str,
        voice_profile: dict[str, object] | None = None,
    ) -> Submission:
        self.batch_calls.append(lines)
        self.batch_profiles.append(voice_profile)
        if self.unknown:
            raise SubmissionUnknown("lost receipt")
        return Submission(f"{self.kind}-batch", self.kind)

    async def submit(self, *, prompt: str, ordinal: int, operation_key: str) -> Submission:
        self.calls.append(ordinal)
        if self.unknown:
            raise SubmissionUnknown("lost receipt")
        await self.release.wait()
        return Submission(f"{self.kind}-{ordinal}", self.kind)

    async def poll(self, request_id: str) -> AssetResult:
        return AssetResult("ready", url=f"http://127.0.0.1/{request_id}")


async def prepared(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'assets.db'}")
    async with engine.begin() as con:
        await con.run_sync(Base.metadata.create_all)
    store = StudioStore(
        async_sessionmaker(engine, expire_on_commit=False),
        StudioSettings(enabled=True, active_runs=2, media_root=tmp_path / "media"),
    )
    visitor, _ = await store.new_visitor("127.0.0.1")
    run_id = await store.admit(
        visitor_id=visitor.id, item_text="cake", start_key="start-key-1", ip="127.0.0.1"
    )
    plan = deterministic_fixture_plan(run_id, "cake")
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        assert run
        run.status, run.plan, run.plan_hash, run.plan_approved_hash = (
            "generating",
            plan.model_dump(mode="json"),
            plan.canonical_sha256,
            plan.canonical_sha256,
        )
        # This helper constructs archived v1 behavior from a newly admitted
        # fixture row. Make the legacy pipeline identity explicit.
        run.execution_profile = None
        session.add(
            StudioDecision(
                run_id=run_id,
                visitor_id=visitor.id,
                gate="ideas",
                revision=1,
                subject_hash=plan.canonical_sha256,
                request_key="decision-key-1",
                decision="approve",
                payload={},
                expires_at=_utc_now() + timedelta(hours=1),
            )
        )
    return store, engine, run_id


def pipeline(store, video, voice, *, preflight=None):  # type: ignore[no-untyped-def]
    return MonthlyAssets(
        store,
        video,
        voice,
        FixtureModerator("allow"),
        MediaFetchPolicy(
            frozenset({"http://127.0.0.1"}), 10_000_000, 100_000_000, fixture_mode=True
        ),
        renderer_preflight=preflight,
    )


@pytest.mark.asyncio
async def test_submit_issues_twelve_videos_and_one_batch_before_completion(tmp_path) -> None:
    store, engine, run_id = await prepared(tmp_path)
    video, voice = Provider("video"), Provider("voice")
    task = asyncio.create_task(pipeline(store, video, voice).submit(run_id))
    # SQLite serializes the tiny durable claim transactions, but none waits for
    # a provider completion. Give that database serialization enough room.
    for _ in range(300):
        if len(video.calls) == 12 and len(voice.batch_calls) == 1:
            break
        await asyncio.sleep(0.01)
    assert sorted(video.calls) == list(range(1, 13))
    assert len(voice.batch_calls) == 1 and len(voice.batch_calls[0]) == 12
    video.release.set()
    voice.release.set()
    await task
    async with store.factory() as session:
        assert (
            len(
                (
                    await session.scalars(select(StudioAsset).where(StudioAsset.run_id == run_id))
                ).all()
            )
            == 13
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_scene_batch_persists_pinned_profile_and_exact_title_text_before_submit(
    tmp_path,
) -> None:
    store, engine, run_id = await prepared(tmp_path)
    plan = deterministic_scene_plan(run_id, "cake")
    profile = build_execution_profile()
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        assert run is not None
        run.plan = plan.model_dump(mode="json")
        run.plan_hash = plan.canonical_sha256
        run.plan_approved_hash = plan.canonical_sha256
        run.execution_profile = profile
        approval = await session.scalar(
            select(StudioDecision).where(StudioDecision.run_id == run_id)
        )
        assert approval is not None
        approval.subject_hash = plan.canonical_sha256

    video, voice = Provider("video"), Provider("voice")
    video.release.set()
    await pipeline(store, video, voice).submit(run_id)

    assert len(voice.batch_calls) == 1
    assert voice.batch_calls[0] == tuple(f"{scene.title}." for scene in plan.scenes)
    assert voice.batch_profiles == [profile["voice_profile"]]
    async with store.factory() as session:
        batch = await session.scalar(
            select(StudioAsset).where(
                StudioAsset.run_id == run_id,
                StudioAsset.kind == "voice_batch",
            )
        )
        assert batch is not None
        metadata = batch.media_metadata
        assert metadata["voice_profile"] == profile["voice_profile"]
        assert metadata["voice_profile_sha256"] == profile["voice_profile_sha256"]
        assert metadata["submitted_text"].startswith("[whispers] ")
        assert all(
            month not in metadata["submitted_text"]
            for month in (
                "January",
                "February",
                "March",
                "April",
                "May",
                "June",
                "July",
                "August",
                "September",
                "October",
                "November",
                "December",
            )
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_fixture_scene_batch_gather_accepts_explicit_fake_model_and_creates_title_rows(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine, run_id = await prepared(tmp_path)
    plan = deterministic_scene_plan(run_id, "cake")
    profile = build_execution_profile()
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        assert run is not None
        run.plan = plan.model_dump(mode="json")
        run.plan_hash = plan.canonical_sha256
        run.plan_approved_hash = plan.canonical_sha256
        run.execution_profile = profile
        approval = await session.scalar(
            select(StudioDecision).where(StudioDecision.run_id == run_id)
        )
        assert approval is not None
        approval.subject_hash = plan.canonical_sha256

    video, voice = Provider("video"), Provider("voice")
    video.release.set()
    assets = pipeline(store, video, voice)
    await assets.submit(run_id)
    async with store.transaction() as session:
        batch = await session.scalar(
            select(StudioAsset).where(
                StudioAsset.run_id == run_id, StudioAsset.kind == "voice_batch"
            )
        )
        assert batch is not None
        batch.model = FAKE_NARRATION_BATCH_MODEL
        batch.request_id = "fake-title-batch"
        batch.status = "queued"
        batch_id = batch.id

    lines = tuple(f"{scene.title}." for scene in plan.scenes)
    text = build_scene_narration_text(lines)
    timestamps = (
        {
            "characters": list(text),
            "character_start_times_seconds": [index / 100 for index in range(len(text))],
            "character_end_times_seconds": [(index + 1) / 100 for index in range(len(text))],
        },
    )

    async def fake_fetch(**_kwargs):  # type: ignore[no-untyped-def]
        return VerifiedMedia(
            path=tmp_path / "title-batch.wav",
            sha256="a" * 64,
            byte_size=100,
            duration_seconds=12.0,
            width=0,
            height=0,
            frame_paths=(),
            metadata={},
        )

    async def fake_split(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return [tmp_path / f"scene-{ordinal:02d}-title.wav" for ordinal in range(1, 13)]

    async def fake_silences(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return ()

    async def fake_verify(path, *, kind):  # type: ignore[no-untyped-def]
        assert kind == "audio"
        return VerifiedMedia(
            path=path,
            sha256=f"{int(path.name[6:8]):064x}",
            byte_size=10,
            duration_seconds=0.5,
            width=0,
            height=0,
            frame_paths=(),
            metadata={},
        )

    def fake_pause_splits(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return tuple(VoiceSplit(index, line, 0.0, 0.5) for index, line in enumerate(lines, 1))

    monkeypatch.setattr(asset_module, "fetch_verified_media", fake_fetch)
    monkeypatch.setattr(asset_module, "verify_media_file", fake_verify)
    monkeypatch.setattr("myloware.studio.voice_batch.split_wav", fake_split)
    monkeypatch.setattr("myloware.studio.voice_pauses.detect_silences", fake_silences)
    monkeypatch.setattr("myloware.studio.voice_pauses.plan_pause_splits", fake_pause_splits)
    await assets._gather_batch(
        batch_id,
        AssetResult("ready", url="http://127.0.0.1/title-batch.wav", timestamps=timestamps),
    )

    async with store.factory() as session:
        batch = await session.get(StudioAsset, batch_id)
        rows = (
            await session.scalars(
                select(StudioAsset).where(StudioAsset.run_id == run_id, StudioAsset.kind == "voice")
            )
        ).all()
        assert batch is not None and batch.status == "ready", batch.error_code
        assert len(rows) == 12
        assert {row.model for row in rows} == {FAKE_NARRATION_BATCH_MODEL}
        assert [row.input_hash for row in sorted(rows, key=lambda row: row.ordinal)] == [
            sha256(line.encode()).hexdigest() for line in lines
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_local_split_rows_keep_requested_hash_and_saved_source_provenance(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine, run_id = await prepared(tmp_path)
    plan = deterministic_scene_plan(run_id, "cake")
    profile = build_execution_profile()
    requested_lines = tuple(f"{scene.title}." for scene in plan.scenes)
    source_lines = tuple(f"Saved {ordinal} Teacup." for ordinal in range(1, 13))
    receipt = {
        "archive_id": "test-local-replay",
        "manifest_sha256": "f" * 64,
        "local_batch_sha256": "a" * 64,
    }
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        approval = await session.scalar(
            select(StudioDecision).where(StudioDecision.run_id == run_id)
        )
        assert run is not None and approval is not None
        run.mode = "local"
        run.plan = plan.model_dump(mode="json")
        run.plan_hash = run.plan_approved_hash = plan.canonical_sha256
        run.execution_profile = profile
        approval.subject_hash = plan.canonical_sha256
        batch = StudioAsset(
            run_id=run_id,
            revision=1,
            ordinal=0,
            kind="voice_batch",
            attempt=1,
            input_hash=sha256("\n".join(requested_lines).encode()).hexdigest(),
            operation_key="local-title-batch",
            provider="local",
            model=LOCAL_SCENE_MEDIA_MODEL,
            request_id="local-scene-title-batch-1234567890abcdef",
            status="queued",
            media_metadata={
                "alignment_status": "ordinal_only_mismatch",
                "requested_title_lines": list(requested_lines),
                "requested_title_lines_sha256": sha256(
                    "\n".join(requested_lines).encode()
                ).hexdigest(),
                "source_title_lines": list(source_lines),
                "source_title_text_sha256": sha256("\n\n".join(source_lines).encode()).hexdigest(),
                "local_media_receipt": receipt,
                "voice_profile": profile["voice_profile"],
                "voice_profile_sha256": profile["voice_profile_sha256"],
            },
        )
        session.add(batch)
        await session.flush()
        batch_id = batch.id

    async def fake_fetch(**_kwargs):  # type: ignore[no-untyped-def]
        return VerifiedMedia(
            path=tmp_path / "title-batch.wav",
            sha256="a" * 64,
            byte_size=100,
            duration_seconds=12.0,
            width=0,
            height=0,
            frame_paths=(),
            metadata={},
        )

    async def fake_split(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return [tmp_path / f"scene-{ordinal:02d}-title.wav" for ordinal in range(1, 13)]

    async def fake_silences(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return ()

    async def fake_verify(path, *, kind):  # type: ignore[no-untyped-def]
        return VerifiedMedia(
            path=path,
            sha256=f"{int(path.name[6:8]):064x}",
            byte_size=10,
            duration_seconds=0.5,
            width=0,
            height=0,
            frame_paths=(),
            metadata={},
        )

    def fake_pause_splits(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return tuple(
            VoiceSplit(index, line, 0.0, 0.5) for index, line in enumerate(source_lines, 1)
        )

    monkeypatch.setattr(asset_module, "fetch_verified_media", fake_fetch)
    monkeypatch.setattr(asset_module, "verify_media_file", fake_verify)
    monkeypatch.setattr("myloware.studio.voice_batch.split_wav", fake_split)
    monkeypatch.setattr("myloware.studio.voice_pauses.detect_silences", fake_silences)
    monkeypatch.setattr("myloware.studio.voice_pauses.plan_pause_splits", fake_pause_splits)
    assets = pipeline(store, Provider("video"), Provider("voice"))
    await assets._gather_batch(
        batch_id,
        AssetResult(
            "ready",
            url="http://127.0.0.1/title-batch.wav",
            timestamps=(
                {
                    "characters": list("\n\n".join(source_lines)),
                    "character_start_times_seconds": [
                        index / 100 for index in range(len("\n\n".join(source_lines)))
                    ],
                    "character_end_times_seconds": [
                        (index + 1) / 100 for index in range(len("\n\n".join(source_lines)))
                    ],
                },
            ),
            input_text="\n\n".join(source_lines),
            local_media={"archive": receipt, "source_batch_sha256": "a" * 64},
        ),
    )
    async with store.transaction() as session:
        for scene, source_line in zip(plan.scenes, source_lines, strict=True):
            video_hash = sha256(f"video-{scene.ordinal}".encode()).hexdigest()
            session.add(
                StudioAsset(
                    run_id=run_id,
                    revision=1,
                    ordinal=scene.ordinal,
                    kind="video",
                    attempt=1,
                    input_hash=sha256(scene.visual_prompt.encode()).hexdigest(),
                    operation_key=f"video-{scene.ordinal}",
                    provider="local",
                    model=LOCAL_SCENE_MEDIA_MODEL,
                    request_id=f"local-scene-video-{scene.ordinal:02d}-1234567890abcdef",
                    status="ready",
                    artifact_id=uuid4(),
                    sha256=video_hash,
                    media_metadata={
                        "alignment_status": "ordinal_only_mismatch",
                        "source_title": source_line,
                        "source_title_sha256": sha256(source_line.encode()).hexdigest(),
                        "source_sha256": video_hash,
                        "local_media_receipt": receipt,
                    },
                    safety={"safe": True, "media_sha256": video_hash},
                )
            )
    async with store.factory() as session:
        selected_assets = (
            await session.scalars(select(StudioAsset).where(StudioAsset.run_id == run_id))
        ).all()
        voices = sorted(
            (row for row in selected_assets if row.kind == "voice"), key=lambda row: row.ordinal
        )
        assert [row.input_hash for row in voices] == [
            sha256(line.encode()).hexdigest() for line in requested_lines
        ]
        assert [row.media_metadata["source_title"] for row in voices] == list(source_lines)
        assert validate_scene_assets(plan, profile, selected_assets, mode="local")
    await engine.dispose()


async def _make_live_scene_run(tmp_path):  # type: ignore[no-untyped-def]
    store, engine, run_id = await prepared(tmp_path)
    plan = deterministic_scene_plan(run_id, "cake")
    profile = build_execution_profile()
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        assert run is not None
        run.mode = "live"
        run.plan = plan.model_dump(mode="json")
        run.plan_hash = plan.canonical_sha256
        run.plan_approved_hash = plan.canonical_sha256
        run.execution_profile = profile
        approval = await session.scalar(
            select(StudioDecision).where(StudioDecision.run_id == run_id)
        )
        assert approval is not None
        approval.subject_hash = plan.canonical_sha256
    return store, engine, run_id, profile


@pytest.mark.asyncio
async def test_live_scene_preflight_runs_before_any_intent_or_provider_call(tmp_path) -> None:
    store, engine, run_id, profile = await _make_live_scene_run(tmp_path)
    video, voice = Provider("video"), Provider("voice")
    preflights: list[dict[str, object]] = []

    async def preflight(value: dict[str, object]) -> None:
        preflights.append(value)

    video.release.set()
    await pipeline(store, video, voice, preflight=preflight).submit(run_id)
    assert preflights == [profile]
    assert len(video.calls) == 12 and len(voice.batch_calls) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_live_scene_preflight_failure_creates_no_intents_or_paid_calls(tmp_path) -> None:
    store, engine, run_id, _ = await _make_live_scene_run(tmp_path)
    video, voice = Provider("video"), Provider("voice")

    async def unavailable(_value: dict[str, object]) -> None:
        raise RuntimeError("preset unavailable")

    with pytest.raises(Exception, match="scene_renderer_preflight_failed"):
        await pipeline(store, video, voice, preflight=unavailable).submit(run_id)
    assert not video.calls and not voice.batch_calls
    async with store.factory() as session:
        assert not (
            await session.scalars(select(StudioAsset).where(StudioAsset.run_id == run_id))
        ).all()
    await engine.dispose()


@pytest.mark.asyncio
async def test_tampered_live_scene_profile_fails_before_preflight_or_provider_call(
    tmp_path,
) -> None:
    store, engine, run_id, _ = await _make_live_scene_run(tmp_path)
    video, voice = Provider("video"), Provider("voice")
    calls = 0

    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        assert run is not None and run.execution_profile is not None
        profile = dict(run.execution_profile)
        profile["voice_profile_sha256"] = "0" * 64
        run.execution_profile = profile

    async def preflight(_value: dict[str, object]) -> None:
        nonlocal calls
        calls += 1

    with pytest.raises(Exception, match="scene_execution_profile_invalid"):
        await pipeline(store, video, voice, preflight=preflight).submit(run_id)
    assert calls == 0 and not video.calls and not voice.batch_calls
    await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_submit_and_unknown_restart_never_blindly_resubmit(
    tmp_path,
) -> None:
    store, engine, run_id = await prepared(tmp_path)
    video, voice = Provider("video", unknown=True), Provider("voice", unknown=True)
    assets = pipeline(store, video, voice)
    await assets.submit(run_id)
    assert len(video.calls) == 12 and len(voice.batch_calls) == 1
    await assets.submit(run_id)
    assert len(video.calls) == 12 and len(voice.batch_calls) == 1
    async with store.factory() as session:
        states = (
            await session.scalars(select(StudioAsset.status).where(StudioAsset.run_id == run_id))
        ).all()
        assert set(states) == {"submission_unknown"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_known_ready_video_with_local_retrieval_failure_is_repolled_not_resubmitted(
    tmp_path, monkeypatch
) -> None:
    store, engine, run_id = await prepared(tmp_path)

    class RecoveringProvider(Provider):
        async def poll(self, request_id: str) -> AssetResult:
            if request_id.startswith("voice-"):
                return AssetResult("unknown")
            return await super().poll(request_id)

    video, voice = RecoveringProvider("video"), RecoveringProvider("voice")
    video.release.set()
    assets = pipeline(store, video, voice)

    async def unavailable_media(**_kwargs):  # type: ignore[no-untyped-def]
        raise MediaVerificationError("temporary local verification failure")

    monkeypatch.setattr(asset_module, "fetch_verified_media", unavailable_media)
    await assets.submit(run_id)
    assert len(video.calls) == 12
    await assets.reconcile(run_id)
    await assets.reconcile(run_id)
    await assets.reconcile(run_id)

    assert len(video.calls) == 12
    async with store.factory() as session:
        rows = (
            await session.scalars(
                select(StudioAsset).where(StudioAsset.run_id == run_id, StudioAsset.kind == "video")
            )
        ).all()
        assert len(rows) == 12
        assert {row.status for row in rows} == {"failed"}
        assert {row.error_code for row in rows} == {"media_retrieval_exhausted"}
        assert {row.media_metadata["retrieval_attempts"] for row in rows} == {3}
    await engine.dispose()


@pytest.mark.asyncio
async def test_known_ready_video_recovers_local_retrieval_without_new_submission(
    tmp_path, monkeypatch
) -> None:
    store, engine, run_id = await prepared(tmp_path)

    class RecoveringProvider(Provider):
        async def poll(self, request_id: str) -> AssetResult:
            if request_id.startswith("voice-"):
                return AssetResult("unknown")
            return await super().poll(request_id)

    video, voice = RecoveringProvider("video"), RecoveringProvider("voice")
    video.release.set()
    assets = pipeline(store, video, voice)
    video_fetches = 0

    async def recovered_media(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal video_fetches
        if kwargs["kind"] != "video":
            raise MediaVerificationError("voice outside this focused probe")
        video_fetches += 1
        if video_fetches <= 12:
            raise MediaVerificationError("temporary local verification failure")
        ordinal = int(kwargs["url"].split("-")[1])
        return VerifiedMedia(
            path=Path(tmp_path / f"video-{ordinal}.mp4"),
            sha256=f"{ordinal:064x}",
            byte_size=100,
            duration_seconds=2.0,
            width=480,
            height=854,
            frame_paths=(Path(tmp_path / "frame.jpg"),),
            metadata={},
        )

    monkeypatch.setattr(asset_module, "fetch_verified_media", recovered_media)
    await assets.submit(run_id)
    await assets.reconcile(run_id)
    await assets.reconcile(run_id)

    assert len(video.calls) == 12
    async with store.factory() as session:
        rows = (
            await session.scalars(
                select(StudioAsset).where(StudioAsset.run_id == run_id, StudioAsset.kind == "video")
            )
        ).all()
        assert len(rows) == 12
        assert {row.status for row in rows} == {"ready"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_live_provider_failure_without_attempt_reservation_never_regenerates(
    tmp_path,
) -> None:
    store, engine, run_id = await prepared(tmp_path)

    class FailedProvider(Provider):
        async def poll(self, request_id: str) -> AssetResult:
            return AssetResult("failed")

    video, voice = FailedProvider("video"), FailedProvider("voice")
    video.release.set()
    assets = pipeline(store, video, voice)
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        assert run is not None
        run.mode = "live"
    await assets.submit(run_id)
    await assets.reconcile(run_id)

    assert len(video.calls) == 12
    async with store.factory() as session:
        rows = (
            await session.scalars(
                select(StudioAsset).where(StudioAsset.run_id == run_id, StudioAsset.kind == "video")
            )
        ).all()
        assert len(rows) == 12
        assert {row.error_code for row in rows} == {"retry_reservation_missing"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_live_provider_failure_retries_only_with_matching_reserved_attempt(tmp_path) -> None:
    store, engine, run_id = await prepared(tmp_path)

    class FailedProvider(Provider):
        async def poll(self, request_id: str) -> AssetResult:
            return AssetResult("failed")

    video, voice = FailedProvider("video"), FailedProvider("voice")
    video.release.set()
    assets = pipeline(store, video, voice)
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        assert run is not None
        run.mode = "live"
        reservation = await session.get(StudioReservation, run_id)
        assert reservation is not None
        reservation.reserved_usd = Decimal(4)
        reservation.cost_state = "reserved"
        reservation.cost_profile_version = "retry-profile"
        session.add(
            StudioCostLedger(
                run_id=run_id,
                profile_version="retry-profile",
                stage="video_request",
                operation_key="month:1:attempt:2",
                reserved_usd=Decimal(4),
                cost_state="reserved",
            )
        )
    await assets.submit(run_id)
    await assets.reconcile(run_id)

    assert len(video.calls) == 13
    async with store.factory() as session:
        retried = await session.scalar(
            select(StudioAsset).where(
                StudioAsset.run_id == run_id,
                StudioAsset.kind == "video",
                StudioAsset.ordinal == 1,
                StudioAsset.attempt == 2,
            )
        )
        assert retried is not None and retried.status == "queued"
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_or_expired_approval_blocks_effects(tmp_path) -> None:
    store, engine, run_id = await prepared(tmp_path)
    async with store.factory() as session:
        receipt = await session.scalar(
            select(StudioDecision).where(StudioDecision.run_id == run_id)
        )
        assert receipt
        receipt.expires_at = _utc_now() - timedelta(seconds=1)
        await session.commit()
    video, voice = Provider("video"), Provider("voice")
    with pytest.raises(Exception, match="stale_review"):
        await pipeline(store, video, voice).submit(run_id)
    assert not video.calls and not voice.calls
    await engine.dispose()


@pytest.mark.asyncio
async def test_out_of_order_video_receipts_do_not_enter_editing_without_batch_proof(
    tmp_path, monkeypatch
) -> None:
    store, engine, run_id = await prepared(tmp_path)

    class ReadyProvider(Provider):
        async def poll(self, request_id: str) -> AssetResult:
            # Completion order is deliberately opposite calendar order.
            await asyncio.sleep((13 - int(request_id.rsplit("-", 1)[1])) / 10000)
            return AssetResult("ready", url=f"http://127.0.0.1/{request_id}")

    video, voice = ReadyProvider("video"), ReadyProvider("voice")
    video.release.set()
    voice.release.set()
    pipeline_instance = pipeline(store, video, voice)

    async def verified(**kwargs):  # type: ignore[no-untyped-def]
        ordinal = int(kwargs["url"].rsplit("-", 1)[1])
        is_video = kwargs["kind"] == "video"
        return VerifiedMedia(
            path=Path(tmp_path / f"{kwargs['kind']}-{ordinal}"),
            sha256=f"{ordinal:064x}",
            byte_size=100,
            duration_seconds=2.0 if is_video else 1.0,
            width=480 if is_video else 0,
            height=854 if is_video else 0,
            frame_paths=(
                (
                    Path(tmp_path / "first.jpg"),
                    Path(tmp_path / "middle.jpg"),
                    Path(tmp_path / "last.jpg"),
                )
                if is_video
                else ()
            ),
            metadata={},
        )

    monkeypatch.setattr(asset_module, "fetch_verified_media", verified)
    await pipeline_instance.submit(run_id)
    await pipeline_instance.reconcile(run_id)
    async with store.factory() as session:
        run = await session.get(StudioRun, run_id)
        assert run and run.status == "generating"
        states = (
            await session.scalars(select(StudioAsset.status).where(StudioAsset.run_id == run_id))
        ).all()
        assert states.count("ready") == 12
        events = (
            await session.scalars(
                select(StudioEvent).where(
                    StudioEvent.run_id == run_id,
                    StudioEvent.event_type == "assets_ready",
                )
            )
        ).all()
        assert len(events) == 0
    await pipeline_instance.reconcile(run_id)
    async with store.factory() as session:
        run = await session.get(StudioRun, run_id)
        assert run and run.status == "generating"
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("current_revision,expected_events", [(1, 1), (2, 0)])
async def test_narration_phase_stays_bound_to_observed_batch_revision(
    tmp_path, monkeypatch, current_revision, expected_events
) -> None:
    store, engine, run_id = await prepared(tmp_path)
    video, voice = Provider("video"), Provider("voice")
    video.release.set()
    assets = pipeline(store, video, voice)
    try:
        await assets.submit(run_id)
        observed_run = await store.get_run(run_id)
        observed_plan = deterministic_fixture_plan(run_id, "cake")
        async with store.transaction() as session:
            current = await session.get(StudioRun, run_id)
            current.revision = current_revision
            batch = await session.scalar(
                select(StudioAsset).where(
                    StudioAsset.run_id == run_id, StudioAsset.kind == "voice_batch"
                )
            )
            batch_id = batch.id

        async def observed_approval(*args):  # type: ignore[no-untyped-def]
            return observed_run, observed_plan

        class PhaseBoundaryReached(BaseException):
            pass

        def stop_before_media(*args):  # type: ignore[no-untyped-def]
            raise PhaseBoundaryReached

        # Model a revision changing after the approved batch was observed,
        # and stop before any media fetch or split in this status-only test.
        monkeypatch.setattr(assets, "_approved_plan", observed_approval)
        monkeypatch.setattr(assets, "_narration_lines", stop_before_media)
        for _ in range(2):
            with pytest.raises(PhaseBoundaryReached):
                await assets._gather_batch(
                    batch_id, AssetResult("ready", url="http://127.0.0.1/batch")
                )
        snapshot = await store.snapshot(run_id, visitor_id=observed_run.visitor_id)
        phases = [
            event
            for event in snapshot["events"]
            if event["type"] == "workflow_phase"
            and event["detail"].get("role") == "narration_assembly"
        ]
        assert len(phases) == expected_events
        assert all(event["detail"]["revision"] == 1 for event in phases)
    finally:
        await engine.dispose()
