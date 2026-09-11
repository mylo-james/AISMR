from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.config.studio import StudioSettings
from myloware.providers.media import AssetResult, Submission
from myloware.storage.models import Base, _utc_now
from myloware.storage.repositories import StaleJobClaim
from myloware.storage.studio_models import (
    StudioAsset,
    StudioCostLedger,
    StudioDecision,
    StudioPlannerRun,
    StudioPlanningOperation,
    StudioRun,
)
from myloware.storage.studio_store import StudioError, StudioStore, digest
from myloware.studio.assets import MonthlyAssets
from myloware.studio.creative_planning import PLANNING_ROLE_LIMITS, CreativePlanner
from myloware.studio.media import MediaFetchPolicy
from myloware.studio.moderation import FixtureModerator
from myloware.studio.planning_store import (
    DurablePlanning,
    _validated_v3_ranked_selection,
    planning_deadline_policy,
    purge_expired_planning_data,
)
from myloware.studio.service import StudioService
from myloware.workers.claims import JobClaim, current_job_claim
from myloware.workflows.langgraph import studio as studio_graph


def _cards(prefix: str, revision: int) -> list[dict[str, str]]:
    return [
        {
            "candidate_id": f"{prefix}_{index}",
            "material": f"{prefix} material {revision} {index}",
            "action": f"{prefix} action {revision} {index}",
            "setting": f"{prefix} setting {index}",
            "hook": "visible transformation",
            "payoff": "clear final shape",
            "object_fit": "The teacup remains recognizable.",
        }
        for index in range(1, 16)
    ]


class _Completions:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create(self, **kwargs):  # type: ignore[no-untyped-def]
        role = (
            kwargs["response_format"]["json_schema"]["name"]
            .removeprefix("aismr_")
            .removesuffix(f"_v{CreativePlanner.schema_version}")
        )
        payload = json.loads(kwargs["messages"][1]["content"])
        self.calls.append(role)
        if role in {"explore_object", "explore_surreal", "replenish"}:
            result = {"cards": _cards(role.split("_")[-1], payload["revision"])}
        elif role in {"curate", "recurate"}:
            result = {
                "ranked": [
                    {
                        "candidate_id": payload["cards"][index]["candidate_id"],
                        "reason": "distinct",
                    }
                    for index in range(len(payload["replacement_ordinals"]))
                ]
            }
        elif role == "write_shots":
            cards = {card["candidate_id"]: card for card in payload["cards"]}
            result = {
                "shots": [
                    {
                        "ordinal": chosen["ordinal"],
                        "candidate_id": chosen["candidate_id"],
                        "title_modifier": f"Shifting{payload['revision']}{chosen['ordinal']}",
                        "title_noun": "Teacup",
                        "storyboard": {
                            "opening_frame": f"A teacup of {cards[chosen['candidate_id']]['material']} rests on a stone ledge, its handle visible.",
                            "action": f"The teacup performs {cards[chosen['candidate_id']]['action']} from its rim to its base.",
                            "payoff_frame": "The displaced rim settles into an intact ring around the base; the cup remains upright.",
                            "camera": "Locked three-quarter close-up, the full handle and base remain inside the frame.",
                            "light_and_texture": "Low side light reveals the physical surface and a crisp contact shadow.",
                        },
                    }
                    for chosen in payload["selected"]
                ]
            }
        else:
            raise AssertionError(role)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))]
        )


def test_valid_short_v3_curate_history_maps_the_ranked_prefix_only() -> None:
    selection = _validated_v3_ranked_selection(
        output={"ranked": [{"candidate_id": "object_2", "reason": "strong"}]},
        request={"metadata": {"schema_version": 3}},
        input_payload={"replacement_ordinals": [2, 3, 9]},
        candidate_ids={"object_2", "object_3", "object_9"},
        role="curate",
    )
    assert selection == [("object_2", 2)]


def test_valid_v5_curate_history_remains_readable() -> None:
    selection = _validated_v3_ranked_selection(
        output={"ranked": [{"candidate_id": "object_2", "reason": "strong"}]},
        request={"metadata": {"schema_version": 5}},
        input_payload={"replacement_ordinals": [2]},
        candidate_ids={"object_2"},
        role="recurate",
    )
    assert selection == [("object_2", 2)]


def _settings() -> StudioSettings:
    return StudioSettings(
        enabled=True,
        mode="live",
        live_enabled=True,
        ideation_backend="openai",
        openai_key="test-key",
        fal_key="test-key",
        render_real=True,
        session_secret="x" * 32,
        daily_budget_usd=Decimal(10000),
        run_reservation_usd=Decimal(1),
        cost_profile_version="creative-profile-v2",
        cost_input_moderation_usd=Decimal(1),
        cost_ideation_usd=Decimal(1),
        cost_plan_moderation_usd=Decimal(1),
        cost_video_request_usd=Decimal(1),
        cost_narration_batch_usd=Decimal(1),
        cost_render_usd=Decimal(1),
        cost_final_moderation_usd=Decimal(1),
        plan_revisions=1,
        visitor_runs_24h=3,
        planner_version="creative-v2",
        openai_ideation_model="creative-test-model",
    )


async def _service(tmp_path):  # type: ignore[no-untyped-def]
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'creative.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    store = StudioStore(async_sessionmaker(engine, expire_on_commit=False), _settings())
    completions = _Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    service = StudioService(
        store,
        moderator=FixtureModerator("allow"),
        creative_planner=CreativePlanner(client, model="creative-test-model"),
    )
    visitor, _ = await store.new_visitor("127.0.0.1")
    run_id = await store.admit(
        visitor_id=visitor.id, item_text="teacup", start_key="creative-start-key", ip="127.0.0.1"
    )
    return engine, store, service, visitor.id, run_id, completions


@pytest.mark.asyncio
async def test_live_v2_plan_revision_receipts_and_cost_roles(tmp_path) -> None:
    engine, store, service, visitor_id, run_id, completions = await _service(tmp_path)
    try:
        await service.ideate(run_id)
        first = await store.get_run(run_id)
        assert first.status == "idea_review" and first.plan_hash
        assert completions.calls == ["explore_object", "explore_surreal", "curate", "write_shots"]
        async with store.factory() as session:
            assert not (
                await session.scalars(select(StudioAsset).where(StudioAsset.run_id == run_id))
            ).all()
            operations = (
                await session.scalars(
                    select(StudioPlanningOperation).where(StudioPlanningOperation.run_id == run_id)
                )
            ).all()
            assert {row.status for row in operations} == {"ready"}
            planner = await session.get(StudioPlannerRun, run_id)
            assert planner and planner.version == "creative-v2"
            ledger = (
                await session.scalars(
                    select(StudioCostLedger).where(StudioCostLedger.run_id == run_id)
                )
            ).all()
            keys = {(row.stage, row.operation_key) for row in ledger}
            assert ("ideation", "revision:1:role:explore_object") in keys
            assert ("input_moderation", "initial") in keys
            assert first.moderation["planner_version"] == "creative-v2"

        before = list(first.plan["scenes"])
        await store.decide(
            run_id=run_id,
            visitor_id=visitor_id,
            gate="ideas",
            revision=1,
            subject_hash=first.plan_hash,
            decision="revise",
            decision_key="creative-revise-key",
            revision_feedback={"replace_ordinals": [10, 11, 12], "reason": "repetitive"},
        )
        await service.ideate(run_id)
        revised = await store.get_run(run_id)
        assert revised.status == "idea_review" and revised.revision == 2
        assert revised.plan["scenes"][:9] == before[:9]
        assert revised.plan["scenes"][9:] != before[9:]
        assert len(completions.calls) == 8
        assert {role: completions.calls.count(role) for role in set(completions.calls)} == {
            "explore_object": 2,
            "explore_surreal": 2,
            "curate": 2,
            "write_shots": 2,
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_approved_storyboard_reaches_video_provider_without_rewriting(tmp_path) -> None:
    engine, store, service, visitor_id, run_id, _completions = await _service(tmp_path)
    video_prompts: dict[int, str] = {}

    class MediaRecorder:
        async def submit(self, *, prompt: str, ordinal: int, operation_key: str) -> Submission:
            video_prompts[ordinal] = prompt
            return Submission(f"video-{ordinal}", "recording-test-provider")

        async def submit_batch(
            self, *, lines: tuple[str, ...], operation_key: str, voice_profile=None
        ) -> Submission:
            return Submission("narration-batch", "recording-test-provider")

        async def poll(self, request_id: str) -> AssetResult:
            return AssetResult("ready", url=f"https://example.invalid/{request_id}")

    try:
        await service.ideate(run_id)
        run = await store.get_run(run_id)
        expected = {idea["ordinal"]: idea["visual_prompt"] for idea in run.plan["scenes"]}
        assert all(
            "stone ledge" in prompt and "contact shadow" in prompt for prompt in expected.values()
        )
        await store.decide(
            run_id=run_id,
            visitor_id=visitor_id,
            gate="ideas",
            revision=run.revision,
            subject_hash=run.plan_hash,
            decision="approve",
            decision_key="approve-storyboard-handoff",
        )
        provider = MediaRecorder()

        async def renderer_ready(_profile):
            return None

        assets = MonthlyAssets(
            store,
            provider,
            provider,
            FixtureModerator("allow"),
            MediaFetchPolicy(
                allowed_origins=frozenset({"https://example.invalid"}),
                asset_byte_cap=1_000_000,
                run_byte_cap=24_000_000,
            ),
            renderer_preflight=renderer_ready,
        )
        await assets.submit(run_id)
        assert video_prompts == expected
        async with store.factory() as session:
            rows = (
                await session.scalars(
                    select(StudioAsset).where(
                        StudioAsset.run_id == run_id, StudioAsset.kind == "video"
                    )
                )
            ).all()
            assert len(rows) == 12
            assert all(
                row.input_hash == sha256(expected[row.ordinal].encode()).hexdigest() for row in rows
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_durable_planning_replays_ready_receipts_and_holds_uncertain_intent(tmp_path) -> None:
    engine, store, _service_instance, _visitor_id, run_id, completions = await _service(tmp_path)
    try:
        operations = DurablePlanning(store, run_id, 1)
        await operations.context()
        kwargs = _operation_kwargs(run_id)
        produced = 0

        async def producer():
            nonlocal produced
            produced += 1
            return {"cards": []}

        first = await operations(**kwargs, producer=producer)
        replay = await operations(**kwargs, producer=producer)
        assert first == replay == {"cards": []}
        assert produced == 1

        surreal_kwargs = _operation_kwargs(run_id, "explore_surreal")
        async with store.transaction() as session:
            operation = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == run_id,
                    StudioPlanningOperation.revision == 1,
                    StudioPlanningOperation.role == "explore_surreal",
                )
            )
            assert operation is None
            session.add(
                StudioPlanningOperation(
                    run_id=run_id,
                    revision=1,
                    role="explore_surreal",
                    input_hash=digest(
                        {
                            key: value
                            for key, value in surreal_kwargs.items()
                            if key != "operation_name"
                        }
                    ),
                    request={},
                    status="submitting",
                    deadline_at=_utc_now() + timedelta(seconds=30),
                    expires_at=_utc_now() + timedelta(days=1),
                )
            )
        with pytest.raises(StudioError, match="creative_submission_unknown"):
            await operations(
                **surreal_kwargs,
                producer=producer,
            )
        assert produced == 1 and completions.calls == []
        current = await store.get_run(run_id)
        assert current.status == "submission_unknown"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_planning_profile_and_current_revision_are_checked_before_producer(tmp_path) -> None:
    engine, store, service, _visitor_id, run_id, _completions = await _service(tmp_path)
    assert service is not None
    try:
        operations = DurablePlanning(store, run_id, 1)
        await operations.context()
        called = False

        async def producer():
            nonlocal called
            called = True
            return {"cards": []}

        wrong_model = _operation_kwargs(run_id)
        wrong_model["model"] = "wrong-model"
        with pytest.raises(StudioError, match="planner_profile_mismatch"):
            await operations(**wrong_model, producer=producer)
        assert called is False

        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            assert run is not None
            run.revision = 2
        with pytest.raises(StudioError, match="planning_no_longer_current"):
            await operations.context()
        assert called is False
    finally:
        await engine.dispose()


def _operation_kwargs(run_id, role: str = "explore_object"):  # type: ignore[no-untyped-def]
    input_payload = {"test": True}
    return {
        "operation_name": role,
        "input_payload": input_payload,
        "response_schema": {},
        "model": "creative-test-model",
        "prompt": {"system": "test", "user": "test"},
        "limits": {**PLANNING_ROLE_LIMITS[role], "deadline_seconds": 30},
        "metadata": {
            "profile_version": "creative-v2",
            "run_id": str(run_id),
            "revision": 1,
            "prompt_version": CreativePlanner.prompt_version,
            "schema_version": CreativePlanner.schema_version,
            "operation_key": f"{run_id}:1:{role}",
            "input_sha256": sha256(
                json.dumps(input_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        },
    }


@pytest.mark.asyncio
async def test_planning_checks_cancelled_claim_and_missing_reservation_before_producer(
    tmp_path,
) -> None:
    engine, store, _service_instance, _visitor_id, run_id, _completions = await _service(tmp_path)
    try:
        operations = DurablePlanning(store, run_id, 1)
        await operations.context()
        called = 0

        async def producer():
            nonlocal called
            called += 1
            return {"cards": []}

        async with store.transaction() as session:
            run = await session.get(StudioRun, run_id)
            assert run is not None
            run.cancelled, run.status = True, "cancelled"
        with pytest.raises(StudioError, match="planning_no_longer_current"):
            await operations(**_operation_kwargs(run_id), producer=producer)
        assert called == 0

        engine2, store2, _service2, _visitor2, run2, _completions2 = await _service(
            tmp_path / "claim"
        )
        try:
            claim_token = current_job_claim.set(
                JobClaim(job_id=run2, worker_id="lost", generation=1)
            )
            try:
                with pytest.raises(StaleJobClaim):
                    await DurablePlanning(store2, run2, 1).context()
            finally:
                current_job_claim.reset(claim_token)

            operations2 = DurablePlanning(store2, run2, 1)
            await operations2.context()
            async with store2.transaction() as session:
                await session.execute(
                    delete(StudioCostLedger).where(
                        StudioCostLedger.run_id == run2,
                        StudioCostLedger.stage == "ideation",
                        StudioCostLedger.operation_key == "revision:1:role:explore_object",
                    )
                )
            with pytest.raises(StudioError, match="planning_reservation_missing"):
                await operations2(**_operation_kwargs(run2), producer=producer)
            assert called == 0
        finally:
            await engine2.dispose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_workflow_deadline_policy_is_pinned_and_legacy_receipts_use_120_seconds(
    tmp_path,
) -> None:
    engine, store, _service_instance, visitor_id, run_id, _completions = await _service(tmp_path)
    try:
        async with store.factory() as session:
            profile = await session.get(StudioPlannerRun, run_id)
            assert profile is not None
            assert profile.configuration["deadline_seconds"] == 120
            assert profile.configuration["creative_workflow_deadline_seconds"] == 660
            assert profile.configuration["deadline_policy_sha256"] == digest(
                planning_deadline_policy(
                    deadline_seconds=120,
                    creative_workflow_deadline_seconds=660,
                )
            )
        await DurablePlanning(store, run_id, 1).context()
        async with store.factory() as session:
            operation = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == run_id,
                    StudioPlanningOperation.role == "prepare_context",
                )
            )
            assert operation is not None and operation.completed_at is not None
            assert (operation.deadline_at - operation.completed_at).total_seconds() == 660

        legacy_run_id = await store.admit(
            visitor_id=visitor_id,
            item_text="teacup",
            start_key="legacy-deadline-start-key",
            ip="127.0.0.1",
        )
        async with store.transaction() as session:
            legacy = await session.get(StudioPlannerRun, legacy_run_id)
            assert legacy is not None
            legacy.configuration = {
                key: value
                for key, value in legacy.configuration.items()
                if key not in {"creative_workflow_deadline_seconds", "deadline_policy_sha256"}
            }
        store.config.creative_workflow_deadline_seconds = 1
        await DurablePlanning(store, legacy_run_id, 1).context()
        async with store.factory() as session:
            operation = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == legacy_run_id,
                    StudioPlanningOperation.role == "prepare_context",
                )
            )
            assert operation is not None and operation.completed_at is not None
            assert (operation.deadline_at - operation.completed_at).total_seconds() == 120
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_planning_deadline_survives_restart_and_unknown_marks_ledger(tmp_path) -> None:
    engine, store, _service_instance, _visitor_id, run_id, _completions = await _service(tmp_path)
    try:
        operations = DurablePlanning(store, run_id, 1)
        await operations.context()
        async with store.transaction() as session:
            context = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == run_id,
                    StudioPlanningOperation.role == "prepare_context",
                )
            )
            assert context is not None
            context.deadline_at = _utc_now() - timedelta(seconds=1)
        restarted = DurablePlanning(store, run_id, 1)
        called = False

        async def producer():
            nonlocal called
            called = True
            return {"cards": []}

        with pytest.raises(StudioError, match="planning_deadline_exceeded"):
            await restarted(**_operation_kwargs(run_id), producer=producer)
        assert called is False
    finally:
        await engine.dispose()

    engine, store, _service_instance, _visitor_id, run_id, _completions = await _service(
        tmp_path / "unknown"
    )
    try:
        operations = DurablePlanning(store, run_id, 1)
        await operations.context()

        async def provider_failure():
            raise RuntimeError("provider disconnected after submission")

        with pytest.raises(RuntimeError, match="provider disconnected"):
            await operations(**_operation_kwargs(run_id), producer=provider_failure)
        async with store.factory() as session:
            ledger = await session.scalar(
                select(StudioCostLedger).where(
                    StudioCostLedger.run_id == run_id,
                    StudioCostLedger.stage == "ideation",
                    StudioCostLedger.operation_key == "revision:1:role:explore_object",
                )
            )
            operation = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == run_id,
                    StudioPlanningOperation.role == "explore_object",
                )
            )
            assert ledger is not None and ledger.cost_state == "submission_unknown"
            assert operation is not None and operation.status == "unknown"
        assert (await store.get_run(run_id)).status == "submission_unknown"

        replayed = False

        async def must_not_replay():
            nonlocal replayed
            replayed = True
            return {"cards": []}

        with pytest.raises(StudioError, match="planning_no_longer_current"):
            await operations(**_operation_kwargs(run_id), producer=must_not_replay)
        assert replayed is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_racing_a_started_planning_call_preserves_its_receipt_without_replay(
    tmp_path,
) -> None:
    engine, store, _service_instance, _visitor_id, run_id, _completions = await _service(tmp_path)
    try:
        operations = DurablePlanning(store, run_id, 1)
        await operations.context()
        calls = 0

        async def started_provider():
            nonlocal calls
            calls += 1
            async with store.transaction() as session:
                run = await session.get(StudioRun, run_id)
                assert run is not None
                run.cancelled, run.status = True, "cancelled"
            return {"cards": []}

        assert await operations(**_operation_kwargs(run_id), producer=started_provider) == {
            "cards": []
        }
        async with store.factory() as session:
            operation = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == run_id,
                    StudioPlanningOperation.role == "explore_object",
                )
            )
            assert operation is not None and operation.status == "ready"
        assert (await store.get_run(run_id)).status == "cancelled"

        with pytest.raises(StudioError, match="planning_no_longer_current"):
            await operations(**_operation_kwargs(run_id), producer=started_provider)
        assert calls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recent_context_is_visitor_scoped_and_retention_purges_creative_payloads(
    tmp_path,
) -> None:
    engine, store, service, visitor_id, first_run, _completions = await _service(tmp_path)
    try:
        await service.ideate(first_run)
        first = await store.get_run(first_run)
        await store.decide(
            run_id=first_run,
            visitor_id=visitor_id,
            gate="ideas",
            revision=1,
            subject_hash=first.plan_hash,
            decision="revise",
            decision_key="retention-revise-key",
            revision_feedback={"replace_ordinals": [12], "reason": "repetitive"},
        )
        # This finished historical run is eligible as a scoped recent-concept source.
        async with store.transaction() as session:
            prior = await session.get(StudioRun, first_run)
            assert prior is not None
            prior.status = "video_complete"
            curation = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == first_run,
                    StudioPlanningOperation.role == "curate",
                )
            )
            assert curation is not None

        other_visitor, _ = await store.new_visitor("127.0.0.2")
        other_run = await store.admit(
            visitor_id=other_visitor.id,
            item_text="teacup",
            start_key="other-history",
            ip="127.0.0.2",
        )
        await service.ideate(other_run)
        async with store.transaction() as session:
            other = await session.get(StudioRun, other_run)
            assert other is not None
            other.status = "video_complete"
            other_curation = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == other_run,
                    StudioPlanningOperation.role == "curate",
                )
            )
            assert other_curation is not None
            cards = other_curation.request["input_payload"]["cards"]
            for card in cards:
                card["material"] = "foreign visitor material"
            other_curation.request = dict(other_curation.request)

        current_run = await store.admit(
            visitor_id=visitor_id,
            item_text="teacup",
            start_key="same-visitor-history",
            ip="127.0.0.1",
        )
        context = await DurablePlanning(store, current_run, 1).context()
        assert context.recent_concepts
        assert any(concept.material == "object material 1 1" for concept in context.recent_concepts)
        assert any(
            concept.material == "object material 1 12" and concept.outcome == "revised"
            for concept in context.recent_concepts
        )
        assert all(
            concept.material != "foreign visitor material" for concept in context.recent_concepts
        )

        async with store.transaction() as session:
            source = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == first_run,
                    StudioPlanningOperation.role == "curate",
                )
            )
            assert source is not None
            source.expires_at = _utc_now() - timedelta(seconds=1)
            await session.execute(
                delete(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == current_run,
                    StudioPlanningOperation.role == "prepare_context",
                )
            )
        expired_context = await DurablePlanning(store, current_run, 1).context()
        assert expired_context.recent_concepts == ()

        async with store.transaction() as session:
            old_profile = await session.get(StudioPlannerRun, first_run)
            assert old_profile is not None
            old_profile.expires_at = _utc_now() - timedelta(seconds=1)
            old_operations = (
                await session.scalars(
                    select(StudioPlanningOperation).where(
                        StudioPlanningOperation.run_id == first_run
                    )
                )
            ).all()
            for operation in old_operations:
                operation.expires_at = _utc_now() - timedelta(seconds=1)
            await purge_expired_planning_data(session, now=_utc_now())
        async with store.factory() as session:
            decision = await session.scalar(
                select(StudioDecision).where(
                    StudioDecision.run_id == first_run,
                    StudioDecision.decision == "revise",
                )
            )
            operations = (
                await session.scalars(
                    select(StudioPlanningOperation).where(
                        StudioPlanningOperation.run_id == first_run
                    )
                )
            ).all()
            assert decision is not None
            assert (
                "previous_plan" not in decision.payload
                and "revision_feedback" not in decision.payload
            )
            assert all(row.request == {} and row.response is None for row in operations)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_rank", ["overflow", "duplicate"])
async def test_invalid_v3_ranked_history_is_skipped_whole(tmp_path, invalid_rank: str) -> None:
    engine, store, service, visitor_id, prior_run, _completions = await _service(tmp_path)
    try:
        await service.ideate(prior_run)
        async with store.transaction() as session:
            prior = await session.get(StudioRun, prior_run)
            assert prior is not None
            prior.status = "video_complete"
            curation = await session.scalar(
                select(StudioPlanningOperation).where(
                    StudioPlanningOperation.run_id == prior_run,
                    StudioPlanningOperation.role == "curate",
                )
            )
            assert curation is not None and curation.response is not None
            response = dict(curation.response)
            decoded = json.loads(response["value"])
            ranked = list(decoded["ranked"])
            if invalid_rank == "overflow":
                ranked.append({"candidate_id": "surreal_1", "reason": "overflow"})
            else:
                ranked[-1] = {"candidate_id": ranked[0]["candidate_id"], "reason": "duplicate"}
            response["value"] = json.dumps({**decoded, "ranked": ranked})
            curation.response = response

        current_run = await store.admit(
            visitor_id=visitor_id,
            item_text="teacup",
            start_key=f"invalid-history-{invalid_rank}",
            ip="127.0.0.1",
        )
        context = await DurablePlanning(store, current_run, 1).context()
        assert context.recent_concepts == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_monthly_graph_resumes_interrupted_review_into_targeted_revision(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, store, service, visitor_id, run_id, completions = await _service(tmp_path)
    try:

        @asynccontextmanager
        async def same_service(_builder):  # type: ignore[no-untyped-def]
            yield service

        monkeypatch.setattr(studio_graph, "open_studio_service", same_service)
        graph = studio_graph.build_monthly_graph(MemorySaver())
        config = {"configurable": {"thread_id": f"creative-review:{run_id}"}}
        await graph.ainvoke({"run_id": str(run_id), "status": "ideating", "revision": 1}, config)
        interrupted = await graph.aget_state(config)
        first = await store.get_run(run_id)
        assert first.status == "idea_review"
        assert "review_ideas" in interrupted.next

        await store.decide(
            run_id=run_id,
            visitor_id=visitor_id,
            gate="ideas",
            revision=1,
            subject_hash=first.plan_hash,
            decision="revise",
            decision_key="graph-revision-key",
            revision_feedback={"replace_ordinals": [10, 11, 12], "reason": "repetitive"},
        )
        await graph.ainvoke(Command(resume={"wake": True}), config)
        resumed = await graph.aget_state(config)
        revised = await store.get_run(run_id)
        assert revised.status == "idea_review" and revised.revision == 2
        assert "review_ideas" in resumed.next
        expected_roles = ["curate", "explore_object", "explore_surreal", "write_shots"]
        assert sorted(completions.calls[:4]) == expected_roles
        assert sorted(completions.calls[4:]) == expected_roles
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt_version", "different"),
        ("schema_version", 99),
        ("operation_key", "wrong-operation"),
        ("input_sha256", "0" * 64),
    ],
)
async def test_callback_metadata_is_bound_before_any_effect(tmp_path, field, value) -> None:
    engine, store, _, _, run_id, _ = await _service(tmp_path)
    try:
        operations = DurablePlanning(store, run_id, 1)
        await operations.context()
        kwargs = _operation_kwargs(run_id)
        kwargs["metadata"] = {**kwargs["metadata"], field: value}
        called = False

        async def producer():
            nonlocal called
            called = True
            return {}

        with pytest.raises(StudioError, match="planner_profile_mismatch"):
            await operations(**kwargs, producer=producer)
        assert called is False
    finally:
        await engine.dispose()
