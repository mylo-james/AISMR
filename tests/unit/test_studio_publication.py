from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.config.studio import StudioSettings
from myloware.providers.publishing import PublicationResult
from myloware.storage.models import Base
from myloware.storage.studio_models import StudioAsset, StudioDecision, StudioReservation, StudioRun
from myloware.storage.studio_store import StudioStore
from myloware.studio.moderation import build_moderator
from myloware.studio.publication import MonthlyPublication


class Provider:
    def __init__(self, result: PublicationResult) -> None:
        self.result, self.submits, self.polls = result, 0, 0

    async def submit(self, **kwargs):  # type: ignore[no-untyped-def]
        self.submits += 1
        return self.result

    async def poll(self, post_id: str) -> PublicationResult:
        self.polls += 1
        return self.result

    async def creator_capabilities(self, account_id: str) -> Capabilities:
        return Capabilities(account_id)


class Capabilities:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id

    def validate_publication(
        self,
        *,
        account_id: str,
        privacy: str,
        duration_seconds: float,
        interactions: dict[str, bool],
    ) -> None:
        assert account_id == self.account_id
        assert privacy == "PUBLIC_TO_EVERYONE"
        assert duration_seconds == 30.0
        assert interactions == {
            "allow_comment": False,
            "allow_duet": False,
            "allow_stitch": False,
        }


class Transfer:
    async def transfer(self, *, path: Path, sha256: str, operation_key: str) -> str:
        assert sha256 == __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        return "https://media.example/final.mp4"


async def store_for_publication(tmp_path, *, mode: str = "live"):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'publication.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = StudioSettings(
        enabled=True,
        mode=mode,
        media_root=tmp_path / "media",
        daily_budget_usd="10",
        run_reservation_usd="1",
        fal_key="x",
        zernio_key="x",
        tiktok_account_id="account",
        session_secret="x" * 32,
        render_real=True,
        cost_profile_version="test-v1",
        cost_input_moderation_usd="0.01",
        cost_ideation_usd="0.01",
        cost_plan_moderation_usd="0.01",
        cost_video_request_usd="0.01",
        cost_narration_batch_usd="0.01",
        cost_render_usd="0.01",
        cost_final_moderation_usd="0.01",
    )
    return (
        StudioStore(async_sessionmaker(engine, expire_on_commit=False), config),
        engine,
    )


async def ready_run(store: StudioStore, tmp_path, *, mode="live", account_id="account"):
    visitor, _ = await store.new_visitor("127.0.0.1")
    run_id = await store.admit(
        visitor_id=visitor.id,
        item_text="cake",
        start_key="publication-key",
        ip="127.0.0.1",
    )
    content = b"verified final bytes"
    path = tmp_path / "media" / str(run_id) / "final.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    digest = sha256(content).hexdigest()
    config = {
        "account_id": account_id,
        "caption": "safe caption",
        "privacy": "PUBLIC_TO_EVERYONE",
        "ai_disclosure": True,
    }
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        assert run is not None
        run.status, run.final_hash, run.final_metadata, run.publish_config = (
            "final_review",
            digest,
            {"media_verified": True, "sha256": digest, "duration_seconds": 30.0},
            config,
        )
    approval_hash = store.publication_hash(await store.get_run(run_id))
    assert approval_hash
    # Historical publication adapters retain their own contract tests. The v1
    # visitor store no longer issues this authority, so seed an old receipt.
    async with store.transaction() as session:
        run = await session.get(StudioRun, run_id)
        run.status = "publishing"
        run.approved_publish_hash = approval_hash
        session.add(
            StudioDecision(
                run_id=run_id,
                visitor_id=visitor.id,
                gate="publish",
                revision=1,
                subject_hash=approval_hash,
                decision="approve",
                request_key="publish-decision",
                payload=config,
                expires_at=run.expires_at,
            )
        )
        for ordinal in range(1, 13):
            for kind in ("video", "voice"):
                session.add(
                    StudioAsset(
                        run_id=run_id,
                        revision=1,
                        ordinal=ordinal,
                        kind=kind,
                        attempt=1,
                        input_hash="x",
                        operation_key=f"{ordinal}-{kind}",
                        provider="fixture",
                        status="ready",
                        safety={"safe": True},
                    )
                )
    return run_id, path


@pytest.mark.asyncio
async def test_valid_file_sqlite_receipt_publishes_only_after_exact_bytes(
    tmp_path,
) -> None:
    store, engine = await store_for_publication(tmp_path)
    try:
        run_id, _ = await ready_run(store, tmp_path)
        provider = Provider(
            PublicationResult(
                state="published",
                post_id="zernio-1",
                platform_post_id="7420000000000000001",
                platform_url="https://www.tiktok.com/@aismr/video/7420000000000000001",
            )
        )
        await MonthlyPublication(
            store,
            provider,
            build_moderator(mode="fixture", fixture_outcome="allow"),
            Transfer(),
        ).publish(run_id)
        run = await store.get_run(run_id)
        assert (run.status, run.publish_state, run.tiktok_post_id) == (
            "published",
            "published",
            "7420000000000000001",
        )
        assert provider.submits == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_hash_and_asset_tamper_prevent_provider_call(tmp_path) -> None:
    store, engine = await store_for_publication(tmp_path)
    try:
        run_id, path = await ready_run(store, tmp_path)
        path.write_bytes(b"tampered")
        provider = Provider(PublicationResult(state="accepted", post_id="zernio-1"))
        service = MonthlyPublication(
            store,
            provider,
            build_moderator(mode="fixture", fixture_outcome="allow"),
            Transfer(),
        )
        await service.publish(run_id)
        assert provider.submits == 0
        path.write_bytes(b"verified final bytes")
        async with store.transaction() as session:
            asset = await session.scalar(select(StudioAsset).where(StudioAsset.run_id == run_id))
            assert asset is not None
            asset.safety = {"safe": False}
        await service.publish(run_id)
        assert provider.submits == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_double_publish_never_resubmits_and_unknown_is_held(tmp_path) -> None:
    store, engine = await store_for_publication(tmp_path)
    try:
        run_id, _ = await ready_run(store, tmp_path)
        provider = Provider(PublicationResult(state="unknown", error="network"))
        service = MonthlyPublication(
            store,
            provider,
            build_moderator(mode="fixture", fixture_outcome="allow"),
            Transfer(),
        )
        await service.publish(run_id)
        await service.publish(run_id)
        run = await store.get_run(run_id)
        assert provider.submits == 1
        assert (run.status, run.publish_state, run.error_code) == (
            "submission_unknown",
            "unknown",
            "publication_status_unknown",
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_accepted_missing_url_polls_by_provider_id(tmp_path) -> None:
    store, engine = await store_for_publication(tmp_path)
    try:
        run_id, _ = await ready_run(store, tmp_path)
        provider = Provider(PublicationResult(state="accepted", post_id="zernio-1"))
        service = MonthlyPublication(
            store,
            provider,
            build_moderator(mode="fixture", fixture_outcome="allow"),
            Transfer(),
        )
        await service.publish(run_id)
        run = await store.get_run(run_id)
        assert (
            run.status,
            run.publish_state,
            run.publish_request_id,
            run.tiktok_url,
        ) == (
            "publishing",
            "accepted",
            "zernio-1",
            None,
        )
        await service.reconcile(run_id)
        assert provider.polls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id", ["account", ""])
async def test_fixture_completion_is_explicit_and_never_calls_provider_or_transfer(
    tmp_path,
    account_id,
) -> None:
    store, engine = await store_for_publication(tmp_path, mode="fixture")
    try:
        run_id, _ = await ready_run(store, tmp_path, mode="fixture", account_id=account_id)
        provider = Provider(
            PublicationResult(
                state="published",
                post_id="bad",
                platform_post_id="7420000000000000001",
                platform_url="https://www.tiktok.com/@aismr/video/7420000000000000001",
            )
        )
        await MonthlyPublication(
            store, provider, build_moderator(mode="fixture", fixture_outcome="allow")
        ).reconcile(run_id)
        run = await store.get_run(run_id)
        async with store.factory() as session:
            reservation = await session.get(StudioReservation, run_id)
        assert (run.status, run.publish_state, run.tiktok_post_id, run.tiktok_url) == (
            "simulated_complete",
            "simulated",
            None,
            None,
        )
        assert provider.submits == 0
        assert reservation is not None and str(reservation.confirmed_usd) == "0.000000"
    finally:
        await engine.dispose()
