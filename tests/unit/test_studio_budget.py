from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.config.studio import StudioSettings
from myloware.storage.models import Base, Run
from myloware.storage.studio_models import StudioCostLedger, StudioReservation, StudioRun
from myloware.storage.studio_store import StudioError, StudioStore
from myloware.studio.budget import (
    CostConfigurationError,
    ReservationCost,
    StudioCostProfile,
    admission_totals,
    decide_admission,
    ledger_costs,
    legacy_reservation_costs,
    persistent_admission_paused,
    record_cost_receipt,
    record_whole_run_reservation,
    set_persistent_admission_pause,
    stage_reservations,
    whole_run_reserve,
)

NOW = datetime(2026, 9, 9, tzinfo=UTC)
START = NOW - timedelta(hours=24)


def _profile() -> StudioCostProfile:
    return StudioCostProfile(
        version="owner-approved-v1",
        input_moderation=Decimal(1),
        ideation=Decimal(2),
        plan_moderation=Decimal(3),
        video_request=Decimal(4),
        narration_batch=Decimal(5),
        render=Decimal(6),
        final_moderation=Decimal(7),
    )


def test_whole_run_reserve_includes_every_configured_paid_stage() -> None:
    assert _profile().whole_run_reserve() == Decimal(72)
    assert whole_run_reserve(mode="fixture", profile=None) == Decimal(0)
    assert whole_run_reserve(mode="recorded", profile=None) == Decimal(0)
    assert whole_run_reserve(mode="live", profile=_profile()) == Decimal(72)
    assert whole_run_reserve(mode="live", profile=_profile(), allowed_plan_revisions=1) == Decimal(
        78
    )
    assert len(stage_reservations(_profile(), allowed_plan_revisions=1)) == 21


def test_retry_and_role_planning_reservations_cover_every_new_paid_attempt() -> None:
    planning_calls = ((1, "explore_object"), (1, "curate"))
    rows = stage_reservations(
        _profile(),
        allowed_plan_revisions=1,
        asset_retries=1,
        planning_calls=planning_calls,
    )

    assert _profile().reserve_with_revisions(
        1, asset_retries=1, planning_calls=planning_calls
    ) == Decimal(133)
    assert sum(amount for _, _, amount in rows) == Decimal(133)
    assert ("video_request", "month:1:attempt:2", Decimal(4)) in rows
    assert ("narration_batch", "attempt:2", Decimal(5)) in rows
    assert ("input_moderation", "initial", Decimal(1)) in rows
    assert ("ideation", "revision:1:role:curate", Decimal(2)) in rows


def test_role_planning_rejects_duplicate_roles_and_out_of_range_revisions() -> None:
    with pytest.raises(CostConfigurationError, match="distinct_roles"):
        stage_reservations(
            _profile(),
            allowed_plan_revisions=1,
            planning_calls=((1, "curate"), (1, "curate")),
        )
    with pytest.raises(CostConfigurationError, match="revision_role_pairs"):
        stage_reservations(_profile(), allowed_plan_revisions=1, planning_calls=((3, "curate"),))


def test_creative_repair_call_inventory_is_bounded_and_preserves_the_old_default() -> None:
    base = StudioSettings(mode="live", planner_version="creative-v2", plan_revisions=0)
    assert base.creative_repair_attempts == 0
    assert base.planning_calls() == (
        (1, "explore_object"),
        (1, "explore_surreal"),
        (1, "curate"),
        (1, "write_shots"),
        (1, "replenish"),
        (1, "recurate"),
    )

    configured = StudioSettings(
        mode="live", planner_version="creative-v2", plan_revisions=2, creative_repair_attempts=2
    )
    calls = configured.planning_calls()
    assert len(calls) == 24
    for revision in range(1, 4):
        assert tuple(role for call_revision, role in calls if call_revision == revision) == (
            "explore_object",
            "explore_surreal",
            "curate",
            "write_shots",
            "replenish",
            "recurate",
            "repair_1",
            "repair_2",
        )
    with pytest.raises(ValueError, match="less than or equal to 2"):
        StudioSettings(creative_repair_attempts=3)


def test_repair_reservations_cover_every_call_and_reject_unbounded_role_names() -> None:
    calls = StudioSettings(
        mode="live", planner_version="creative-v2", plan_revisions=2, creative_repair_attempts=2
    ).planning_calls()
    rows = stage_reservations(_profile(), allowed_plan_revisions=2, planning_calls=calls)

    assert _profile().reserve_with_revisions(2, planning_calls=calls) == Decimal(126)
    assert sum(amount for _, _, amount in rows) == Decimal(126)
    assert {
        operation_key
        for stage, operation_key, _ in rows
        if stage == "ideation" and ":role:repair_" in operation_key
    } == {
        f"revision:{revision}:role:repair_{attempt}"
        for revision in range(1, 4)
        for attempt in (1, 2)
    }
    with pytest.raises(CostConfigurationError, match="revision_role_pairs"):
        stage_reservations(_profile(), allowed_plan_revisions=0, planning_calls=((1, "repair_3"),))
    with pytest.raises(CostConfigurationError, match="revision_role_pairs"):
        stage_reservations(_profile(), allowed_plan_revisions=0, planning_calls=((1, "curate2"),))


def test_live_profile_rejects_missing_or_non_positive_stage_budget() -> None:
    with pytest.raises(CostConfigurationError, match="live_cost_profile_missing"):
        whole_run_reserve(mode="live", profile=None)
    profile = StudioCostProfile(**{**_profile().__dict__, "render": Decimal(0)})
    with pytest.raises(CostConfigurationError, match="render"):
        profile.whole_run_reserve()


def test_totals_count_current_confirmed_and_old_unresolved_holds() -> None:
    totals = admission_totals(
        [
            ReservationCost(
                NOW - timedelta(hours=1),
                "confirmed",
                Decimal(8),
                confirmed=Decimal(5),
                confirmed_at=NOW - timedelta(hours=1),
            ),
            ReservationCost(
                NOW - timedelta(days=2),
                "confirmed",
                Decimal(9),
                confirmed=Decimal(9),
                confirmed_at=NOW - timedelta(days=2),
            ),
            ReservationCost(NOW - timedelta(days=3), "unknown", Decimal(4), estimated=Decimal(6)),
            ReservationCost(NOW - timedelta(days=5), "reserved", Decimal(3)),
            ReservationCost(NOW - timedelta(hours=1), "input_rejected", Decimal(100)),
        ],
        current_24h_start=START,
    )

    assert totals.confirmed_current_24h == Decimal(5)
    assert totals.unresolved_holds == Decimal(9)
    assert totals.indeterminate_hold_count == 0


def test_unknown_zero_or_missing_amount_fails_closed_instead_of_counting_as_zero() -> None:
    decision = decide_admission(
        reservations=[
            ReservationCost(NOW - timedelta(days=9), "unknown", Decimal(0)),
            ReservationCost(NOW - timedelta(days=9), "new_unreconciled_state", None),
            ReservationCost(
                NOW - timedelta(hours=1),
                "confirmed",
                Decimal(2),
                confirmed_at=NOW - timedelta(hours=1),
            ),
        ],
        current_24h_start=START,
        global_period_budget=Decimal(100),
        candidate_reserve=Decimal(10),
    )

    assert decision.allowed is False
    assert decision.reason == "unresolved_cost_amount_missing"
    assert decision.totals.unresolved_holds == Decimal(0)
    assert decision.totals.indeterminate_hold_count == 3


def test_decision_adds_candidate_whole_run_reserve_to_consumption_and_holds() -> None:
    reservations = [
        ReservationCost(NOW, "confirmed", Decimal(10), confirmed=Decimal(10), confirmed_at=NOW),
        ReservationCost(NOW - timedelta(days=2), "reserved", Decimal(20)),
    ]

    declined = decide_admission(
        reservations=reservations,
        current_24h_start=START,
        global_period_budget=Decimal(100),
        candidate_reserve=Decimal(71),
    )
    allowed = decide_admission(
        reservations=reservations,
        current_24h_start=START,
        global_period_budget=Decimal(101),
        candidate_reserve=Decimal(71),
    )

    assert declined.reason == "budget_exhausted"
    assert allowed.reason == "allowed"


def test_confirmed_cost_uses_confirmation_time_not_reservation_time() -> None:
    totals = admission_totals(
        [
            ReservationCost(
                NOW - timedelta(hours=25),
                "confirmed",
                Decimal(8),
                confirmed=Decimal(5),
                confirmed_at=NOW,
            )
        ],
        current_24h_start=START,
    )

    assert totals.confirmed_current_24h == Decimal(5)
    assert totals.indeterminate_hold_count == 0


def test_confirmed_cost_without_observed_confirmation_time_fails_closed() -> None:
    totals = admission_totals(
        [ReservationCost(NOW - timedelta(days=2), "confirmed", Decimal(8), confirmed=Decimal(5))],
        current_24h_start=START,
    )

    assert totals.confirmed_current_24h == Decimal(0)
    assert totals.indeterminate_hold_count == 1


def test_legacy_reservation_without_ledger_remains_a_conservative_hold() -> None:
    legacy = type(
        "LegacyReservation",
        (),
        {
            "created_at": NOW - timedelta(days=2),
            "cost_state": "unknown",
            "reserved_usd": Decimal(8),
            "estimated_usd": None,
            "confirmed_usd": None,
        },
    )()

    totals = admission_totals(legacy_reservation_costs([legacy]), current_24h_start=START)
    assert totals.unresolved_holds == Decimal(8)


@pytest.mark.asyncio
async def test_live_admission_counts_unledgered_legacy_reservation_hold(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-admission.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    store = StudioStore(
        async_sessionmaker(engine, expire_on_commit=False),
        StudioSettings(
            enabled=True,
            mode="live",
            active_runs=2,
            visitor_runs_24h=2,
            ip_runs_24h=4,
            daily_budget_usd=100,
            run_reservation_usd=1,
            cost_profile_version="owner-v1",
            cost_input_moderation_usd=1,
            cost_ideation_usd=1,
            cost_plan_moderation_usd=1,
            cost_video_request_usd=1,
            cost_narration_batch_usd=1,
            cost_render_usd=1,
            cost_final_moderation_usd=1,
        ),
    )
    try:
        legacy_visitor, _ = await store.new_visitor("127.0.0.1")
        legacy_run_id = uuid4()
        async with store.transaction() as session:
            session.add(
                Run(
                    id=legacy_run_id,
                    workflow_name="monthly",
                    input="teacup",
                    user_id=legacy_visitor.id,
                    status="cancelled",
                )
            )
            await session.flush()
            session.add(
                StudioRun(
                    run_id=legacy_run_id,
                    visitor_id=legacy_visitor.id,
                    start_key="legacy-reservation",
                    read_token_hash=legacy_run_id.hex * 2,
                    item_id="teacup",
                    item_text="teacup",
                    mode="live",
                    status="cancelled",
                    cancelled=True,
                    expires_at=NOW + timedelta(days=1),
                )
            )
            session.add(
                StudioReservation(
                    run_id=legacy_run_id,
                    visitor_id=legacy_visitor.id,
                    ip_hash="legacy-ip",
                    reserved_usd=Decimal(81),
                    cost_state="unknown",
                    created_at=NOW - timedelta(days=2),
                )
            )
        visitor, _ = await store.new_visitor("127.0.0.2")
        with pytest.raises(StudioError, match="budget_exhausted"):
            await store.admit(
                visitor_id=visitor.id,
                item_text="teacup",
                start_key="new-admission",
                ip="127.0.0.2",
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recorded_reservation_has_versioned_stage_rows_and_receipts(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'costs.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    run_id = uuid4()
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            reserve = await record_whole_run_reservation(
                session,
                run_id=run_id,
                visitor_id="visitor",
                ip_hash="ip-hash",
                profile=_profile(),
                allowed_plan_revisions=1,
            )
            await session.flush()
            reservation = await session.get(StudioReservation, run_id)
            assert reserve == Decimal(78)
            assert reservation.cost_profile_version == "owner-approved-v1"
            entries = (await session.scalars(select(StudioCostLedger))).all()
            assert len(entries) == 21
            await record_cost_receipt(
                session,
                run_id=run_id,
                profile_version="owner-approved-v1",
                stage="video_request",
                operation_key="month:1",
                cost_state="unknown",
                receipt_reference="provider-receipt-digest",
            )
            await record_cost_receipt(
                session,
                run_id=run_id,
                profile_version="owner-approved-v1",
                stage="video_request",
                operation_key="month:2",
                cost_state="confirmed",
                confirmed=Decimal(4),
                confirmed_at=NOW,
                receipt_reference="provider-confirmation-digest",
            )
            await session.flush()
            entry = await session.scalar(
                select(StudioCostLedger).where(StudioCostLedger.operation_key == "month:1")
            )
            assert entry.cost_state == "unknown"
            assert entry.reserved_usd == Decimal(4)
            confirmed_entry = await session.scalar(
                select(StudioCostLedger).where(StudioCostLedger.operation_key == "month:2")
            )
            assert confirmed_entry.confirmed_at == NOW
            projected = ledger_costs(entries)
            totals = admission_totals(projected, current_24h_start=START)
            assert totals.unresolved_holds == Decimal(74)
            assert totals.confirmed_current_24h == Decimal(4)
            assert await persistent_admission_paused(session) is False
            await set_persistent_admission_pause(session, paused=True, reason="operator review")
            await session.flush()
            assert await persistent_admission_paused(session) is True
    finally:
        await engine.dispose()
