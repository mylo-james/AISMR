"""Operator-invoked, transaction-safe housekeeping for the Studio.

This module does not schedule itself, contact providers, retry paid work, or
delete bytes.  The existing fenced ``studio.cleanup`` worker remains the only
filesystem executor and is queued only for a verified final with no unresolved
billable work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from myloware.storage.studio_models import StudioCostLedger, StudioReservation, StudioRun
from myloware.storage.studio_store import StudioStore
from myloware.studio.budget import persistent_admission_paused, set_persistent_admission_pause

UNKNOWN_BILLABLE_STATES = frozenset({"unknown", "submission_unknown"})


@dataclass(frozen=True)
class HousekeepingReceipt:
    """Durable state transitions made by one operator pass."""

    expired_plan_reviews: tuple[UUID, ...]
    expired_final_reviews: tuple[UUID, ...]
    cleanup_queued: tuple[UUID, ...]
    cleanup_deferred_for_billable_uncertainty: tuple[UUID, ...]
    overdue_unknown_cost_entries: tuple[UUID, ...]
    admissions_paused: bool


async def run_studio_housekeeping(
    store: StudioStore, *, now: datetime | None = None
) -> HousekeepingReceipt:
    """Expire review windows and escalate overdue unknown paid effects.

    Call this from an operator command or the existing daily worker loop.  The
    Store transaction serializes the scan with admission and decision writes;
    the returned receipt contains no provider or filesystem effect.
    """
    observed_at = _database_time(now or datetime.now(UTC))
    plan_expired: list[UUID] = []
    final_expired: list[UUID] = []
    cleanup_queued: list[UUID] = []
    cleanup_deferred: list[UUID] = []
    overdue_entries: list[UUID] = []
    admissions_paused = False
    async with store.transaction() as session:
        from myloware.studio.planning_store import purge_expired_planning_data

        await purge_expired_planning_data(
            session,
            now=observed_at.replace(tzinfo=None),
            preserve_artifacts=store.config.preserve_run_artifacts,
        )
        runs = (await session.scalars(select(StudioRun))).all()
        ledgers = (await session.scalars(select(StudioCostLedger))).all()
        reservations = (await session.scalars(select(StudioReservation))).all()
        unknown_run_ids = {
            ledger.run_id for ledger in ledgers if ledger.cost_state in UNKNOWN_BILLABLE_STATES
        }
        unknown_run_ids.update(
            reservation.run_id
            for reservation in reservations
            if reservation.cost_state in UNKNOWN_BILLABLE_STATES
        )
        cutoff = observed_at - timedelta(hours=store.config.unknown_hold_reconcile_hours)
        overdue = [
            ledger
            for ledger in ledgers
            if ledger.cost_state in UNKNOWN_BILLABLE_STATES
            and _database_time(ledger.reconciled_at or ledger.created_at) <= cutoff
        ]
        overdue_legacy = [
            reservation
            for reservation in reservations
            if reservation.cost_state in UNKNOWN_BILLABLE_STATES
            and _database_time(reservation.created_at) <= cutoff
        ]
        overdue_entries.extend(ledger.id for ledger in overdue)
        overdue_entries.extend(reservation.run_id for reservation in overdue_legacy)
        if overdue or overdue_legacy:
            await set_persistent_admission_pause(
                session,
                paused=True,
                reason="operator_reconciliation_required",
            )

        for run in runs:
            expiry = _review_expiry(run, observed_at)
            if expiry is None:
                continue
            gate, code = expiry
            run.cancelled = True
            run.status = "cancelled"
            run.error_code = code
            await store.event(
                session,
                run,
                "housekeeping",
                "review_expired",
                {"gate": gate, "expired_at": observed_at.isoformat()},
            )
            if gate == "ideas":
                plan_expired.append(run.run_id)
                continue
            final_expired.append(run.run_id)
            if run.run_id in unknown_run_ids:
                cleanup_deferred.append(run.run_id)
                await store.event(
                    session,
                    run,
                    "housekeeping",
                    "cleanup_deferred_billable_uncertainty",
                )
            elif _owns_verified_final(store, run):
                try:
                    await store.queue_cleanup(session, run.run_id)
                except ValueError as exc:
                    if str(exc) != "job_already_enqueued":
                        raise
                else:
                    cleanup_queued.append(run.run_id)

        for ledger in overdue:
            run = next((candidate for candidate in runs if candidate.run_id == ledger.run_id), None)
            if run is not None:
                await store.event(
                    session,
                    run,
                    "housekeeping",
                    "cost_reconciliation_overdue",
                    {"ledger_id": str(ledger.id), "cost_state": ledger.cost_state},
                )
        for reservation in overdue_legacy:
            run = next(
                (candidate for candidate in runs if candidate.run_id == reservation.run_id), None
            )
            if run is not None:
                await store.event(
                    session,
                    run,
                    "housekeeping",
                    "cost_reconciliation_overdue",
                    {"source": "legacy_reservation", "cost_state": reservation.cost_state},
                )
        admissions_paused = await persistent_admission_paused(session)
    return HousekeepingReceipt(
        expired_plan_reviews=tuple(plan_expired),
        expired_final_reviews=tuple(final_expired),
        cleanup_queued=tuple(cleanup_queued),
        cleanup_deferred_for_billable_uncertainty=tuple(cleanup_deferred),
        overdue_unknown_cost_entries=tuple(overdue_entries),
        admissions_paused=admissions_paused,
    )


def _review_expiry(run: StudioRun, observed_at: datetime) -> tuple[str, str] | None:
    if run.cancelled:
        return None
    if (
        run.status == "idea_review"
        and run.plan_review_expires_at is not None
        and _database_time(run.plan_review_expires_at) <= observed_at
    ):
        return ("ideas", "plan_review_expired")
    if (
        run.status == "final_review"
        and run.final_review_expires_at is not None
        and _database_time(run.final_review_expires_at) <= observed_at
    ):
        return ("final", "final_review_expired")
    return None


def _owns_verified_final(store: StudioStore, run: StudioRun) -> bool:
    metadata = dict(run.final_metadata or {})
    if metadata.get("media_verified") is not True or not run.final_hash:
        return False
    expected = (store.config.media_root / str(run.run_id) / "final.mp4").resolve(strict=False)
    claimed = metadata.get("path")
    if not isinstance(claimed, str):
        return False
    return Path(claimed).resolve(strict=False) == expected


def _database_time(value: datetime) -> datetime:
    """Treat existing naive database timestamps as UTC for deterministic checks."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
