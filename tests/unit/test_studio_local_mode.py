from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.config.studio import StudioSettings
from myloware.storage.models import Base
from myloware.storage.studio_models import StudioAsset, StudioPlannerRun, StudioRun
from myloware.storage.studio_store import StudioError, StudioStore
from myloware.studio.assets import MonthlyAssets
from myloware.studio.local_scene_media import LocalSceneMediaArchive
from myloware.studio.media import MediaFetchPolicy
from myloware.studio.moderation import FixtureModerator
from myloware.studio.service import StudioService
from myloware.workflows.scenes import deterministic_scene_plan


def _local_media_root(tmp_path: Path) -> Path:
    """Build the smallest complete hash-bound archive without external media."""
    root = tmp_path / "local-media"
    source_lines = [f"Title {ordinal}." for ordinal in range(1, 13)]

    def entry(relative_path: str, ordinal: int, title: str) -> dict[str, object]:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = f"saved-local-{relative_path}".encode()
        path.write_bytes(payload)
        return {
            "ordinal": ordinal,
            "relative_path": relative_path,
            "sha256": sha256(payload).hexdigest(),
            "byte_size": len(payload),
            "duration_seconds": 1.0,
            "source_title": title,
        }

    videos = [
        entry(f"video/{ordinal:02d}-video.mp4", ordinal, source_lines[ordinal - 1][:-1])
        for ordinal in range(1, 13)
    ]
    title_clips = [
        entry(f"title/{ordinal:02d}-title.wav", ordinal, source_lines[ordinal - 1][:-1])
        for ordinal in range(1, 13)
    ]
    batch = entry("voice/title-batch.wav", 0, "")
    source_text = "\n".join(source_lines)
    timestamps_payload = json.dumps(
        {
            "source_title_text_sha256": sha256(source_text.encode()).hexdigest(),
            "timestamps": [{"start": 0, "end": 1}],
        }
    ).encode()
    timestamps_path = root / "receipts/source-title-timestamps.json"
    timestamps_path.parent.mkdir(parents=True, exist_ok=True)
    timestamps_path.write_bytes(timestamps_payload)
    manifest = {
        "contract_version": "local-scene-media-v1",
        "archive_id": "test-local-archive",
        "private_only": True,
        "source_title_lines": source_lines,
        "source_title_lines_sha256": sha256(source_text.encode()).hexdigest(),
        "source_title_text_sha256": sha256(source_text.encode()).hexdigest(),
        "source_archive": {
            "original_batch_sha256": "source-batch",
            "original_timestamps_sha256": "source-timestamps",
            "title_cuts_sha256": "title-cuts",
        },
        "videos": videos,
        "title_clips": title_clips,
        "title_batch": batch,
        "title_timestamp_receipt": {
            "relative_path": "receipts/source-title-timestamps.json",
            "sha256": sha256(timestamps_payload).hexdigest(),
            "byte_size": len(timestamps_payload),
        },
    }
    (root / "local-scene-media-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


class _NoCallLocalProvider:
    def __init__(self, archive: LocalSceneMediaArchive) -> None:
        self.archive = archive
        self.calls = 0

    async def submit(self, **_: object) -> object:
        self.calls += 1
        raise AssertionError("a changed local archive must not reach video submission")

    async def submit_batch(self, **_: object) -> object:
        self.calls += 1
        raise AssertionError("a changed local archive must not reach narration submission")


async def _store(tmp_path, *, mode: str = "local", active_runs: int = 4):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'local.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = StudioSettings(
        enabled=True,
        mode=mode,
        origin="http://127.0.0.1:8452",
        session_secret="s" * 32,
        OPENAI_API_KEY="test-openai-key",
        planner_version="creative-v2",
        ideation_backend="codex",
        local_media_root=_local_media_root(tmp_path) if mode == "local" else None,
        render_real=mode == "local",
        active_runs=active_runs,
        visitor_runs_24h=12,
        ip_runs_24h=20,
    )
    return StudioStore(async_sessionmaker(engine, expire_on_commit=False), config), engine


@pytest.mark.asyncio
async def test_local_admission_pins_archive_receipt_and_idea_approval_generates(tmp_path) -> None:
    store, engine = await _store(tmp_path)
    try:
        visitor, _ = await store.new_visitor("127.0.0.1")
        run_id = await store.admit(
            visitor_id=visitor.id, item_text="teacup", start_key="local-admission", ip="127.0.0.1"
        )
        async with store.factory() as session:
            planner = await session.get(StudioPlannerRun, run_id)
            assert planner is not None
            receipt = planner.configuration["local_media_receipt"]
            assert receipt["archive_id"]
            assert receipt == LocalSceneMediaArchive(store.config.local_media_root).receipt()
        plan = deterministic_scene_plan(run_id, "teacup")
        await store.save_plan(plan, {"input": {"safe": True}, "plan": {"safe": True}})
        await store.decide(
            run_id=run_id,
            visitor_id=visitor.id,
            gate="ideas",
            revision=1,
            subject_hash=plan.canonical_sha256,
            decision="approve",
            decision_key="local-idea-approve",
        )
        assert (await store.get_run(run_id)).status == "generating"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_local_final_is_private_and_gallery_consent_is_rejected(tmp_path) -> None:
    store, engine = await _store(tmp_path)
    try:
        visitor, _ = await store.new_visitor("127.0.0.1")
        run_id = await store.admit(
            visitor_id=visitor.id, item_text="teacup", start_key="local-final", ip="127.0.0.1"
        )
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            assert run is not None
            run.status = "final_review"
            run.final_hash = "a" * 64
            run.final_metadata = {"media_verified": True, "sha256": run.final_hash}
        review_hash = store.final_review_hash(await store.get_run(run_id))
        assert review_hash is not None
        await store.decide(
            run_id=run_id,
            visitor_id=visitor.id,
            gate="final",
            revision=1,
            subject_hash=review_hash,
            decision="approve",
            decision_key="local-private-accept",
        )
        assert (await store.get_run(run_id)).status == "video_complete"

        second_id = await store.admit(
            visitor_id=visitor.id, item_text="teacup", start_key="local-no-gallery", ip="127.0.0.1"
        )
        async with store.transaction() as session:
            run = await session.get(StudioRun, second_id)
            assert run is not None
            run.status = "final_review"
            run.final_hash = "b" * 64
            run.final_metadata = {"media_verified": True, "sha256": run.final_hash}
        with pytest.raises(StudioError, match="gallery_eligibility_unverified"):
            await store.decide(
                run_id=second_id,
                visitor_id=visitor.id,
                gate="final",
                revision=1,
                subject_hash=store.final_review_hash(await store.get_run(second_id)),
                decision="approve",
                decision_key="local-gallery-rejected",
                publish_to_gallery=True,
                visibility_version="recent-creations-v1",
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_local_changed_archive_receipt_blocks_render_before_effect(tmp_path) -> None:
    store, engine = await _store(tmp_path)
    try:
        visitor, _ = await store.new_visitor("127.0.0.1")
        run_id = await store.admit(
            visitor_id=visitor.id, item_text="teacup", start_key="changed-archive", ip="127.0.0.1"
        )
        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            planner = await session.get(StudioPlannerRun, run_id)
            assert run is not None and planner is not None
            run.status = "editing"
            planner.configuration = {
                **planner.configuration,
                "local_media_receipt": {"changed": True},
            }
        service = StudioService(store, moderator=FixtureModerator("allow"))
        with pytest.raises(StudioError, match="local_media_receipt_mismatch"):
            await service.submit_edit(run_id)
        assert (await store.get_run(run_id)).render_submission_state is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_local_changed_archive_receipt_blocks_provider_submission(tmp_path) -> None:
    store, engine = await _store(tmp_path)
    try:
        visitor, _ = await store.new_visitor("127.0.0.1")
        run_id = await store.admit(
            visitor_id=visitor.id,
            item_text="teacup",
            start_key="changed-archive-before-media",
            ip="127.0.0.1",
        )
        plan = deterministic_scene_plan(run_id, "teacup")
        await store.save_plan(plan, {"input": {"safe": True}, "plan": {"safe": True}})
        await store.decide(
            run_id=run_id,
            visitor_id=visitor.id,
            gate="ideas",
            revision=1,
            subject_hash=plan.canonical_sha256,
            decision="approve",
            decision_key="changed-archive-idea-approval",
        )
        async with store.transaction() as session:
            planner = await session.get(StudioPlannerRun, run_id)
            assert planner is not None
            planner.configuration = {
                **planner.configuration,
                "local_media_receipt": {"changed": True},
            }
        provider = _NoCallLocalProvider(LocalSceneMediaArchive(store.config.local_media_root))
        assets = MonthlyAssets(
            store,
            provider,
            provider,
            FixtureModerator("allow"),
            MediaFetchPolicy(
                allowed_origins=frozenset({"http://127.0.0.1:8452"}),
                asset_byte_cap=1024,
                run_byte_cap=4096,
                fixture_mode=True,
            ),
        )
        with pytest.raises(StudioError, match="local_media_receipt_mismatch"):
            await assets.submit(run_id)
        assert provider.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_local_runtime_keeps_legacy_planning_run_plan_only_and_excludes_it_from_slots(
    tmp_path,
) -> None:
    local_store, engine = await _store(tmp_path, active_runs=1)
    store = StudioStore(
        local_store.factory,
        local_store.config.model_copy(update={"mode": "planning", "local_media_root": None}),
    )
    try:
        visitor, _ = await store.new_visitor("127.0.0.1")
        run_id = await store.admit(
            visitor_id=visitor.id, item_text="teacup", start_key="planning-keep", ip="127.0.0.1"
        )
        plan = deterministic_scene_plan(run_id, "teacup")
        await store.save_plan(plan, {"input": {"safe": True}, "plan": {"safe": True}})
        await store.decide(
            run_id=run_id,
            visitor_id=visitor.id,
            gate="ideas",
            revision=1,
            subject_hash=plan.canonical_sha256,
            decision="approve",
            decision_key="planning-keep-ideas",
        )
        assert (await store.get_run(run_id)).status == "plan_complete"
        async with store.factory() as session:
            assert not (
                await session.scalars(select(StudioAsset).where(StudioAsset.run_id == run_id))
            ).all()
        legacy = await local_store.get_run(run_id)
        with pytest.raises(StudioError, match="planning_only"):
            StudioService(local_store, moderator=FixtureModerator("allow")).assets(
                run_id, run=legacy
            )
        local_visitor, _ = await local_store.new_visitor("127.0.0.2")
        local_run = await local_store.admit(
            visitor_id=local_visitor.id,
            item_text="teacup",
            start_key="local-not-blocked-by-planning-review",
            ip="127.0.0.2",
        )
        assert (await local_store.get_run(local_run)).mode == "local"
    finally:
        await engine.dispose()
