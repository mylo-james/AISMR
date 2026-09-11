"""Pure whole-run admission accounting for the AISMR Studio.

This module accepts configured Decimal budgets.  It neither selects provider
prices nor contacts providers.  Live admission fails closed when a profile or
an unresolved hold has no usable positive amount; fixture and recorded modes
remain free.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from myloware.storage.studio_models import StudioCostLedger, StudioReservation

ZERO = Decimal(0)
EXCLUDED_COST_STATES = frozenset({"input_rejected"})


class CostConfigurationError(ValueError):
    """A live cost profile cannot safely reserve a complete run."""


@dataclass(frozen=True)
class StudioCostProfile:
    """Owner-configured, versioned Decimal budgets for every paid live stage."""

    version: str
    input_moderation: Decimal
    ideation: Decimal
    plan_moderation: Decimal
    video_request: Decimal
    narration_batch: Decimal
    render: Decimal
    final_moderation: Decimal

    def whole_run_reserve(
        self,
        *,
        asset_retries: int = 0,
        planning_calls: Sequence[tuple[int, str]] = (),
    ) -> Decimal:
        """Return the conservative amount reserved before paid ideation begins."""
        _require_profile(self)
        _validate_asset_retries(asset_retries)
        _validate_planning_calls(planning_calls)
        initial_roles = _roles_for_revision(planning_calls, 1)
        return (
            self.input_moderation
            + self.ideation * (len(initial_roles) if initial_roles else 1)
            + self.plan_moderation
            + (self.video_request * 12)
            + self.narration_batch
            + self.render
            + self.final_moderation
            + (self.video_request * 12 + self.narration_batch) * asset_retries
        )

    def reserve_with_revisions(
        self,
        allowed_plan_revisions: int,
        *,
        asset_retries: int = 0,
        planning_calls: Sequence[tuple[int, str]] = (),
    ) -> Decimal:
        """Include ideation and plan moderation for every explicitly allowed revision."""
        if allowed_plan_revisions < 0:
            raise CostConfigurationError("allowed_plan_revisions_must_not_be_negative")
        _validate_planning_calls(planning_calls, maximum_revision=allowed_plan_revisions + 1)
        return (
            self.whole_run_reserve(asset_retries=asset_retries, planning_calls=planning_calls)
            + ((self.input_moderation + self.plan_moderation) * allowed_plan_revisions)
            + sum(
                self.ideation * (len(_roles_for_revision(planning_calls, revision)) or 1)
                for revision in range(2, allowed_plan_revisions + 2)
            )
        )


@dataclass(frozen=True)
class ReservationCost:
    """The accounting fields admission needs from one persisted reservation."""

    created_at: datetime
    cost_state: str
    reserved: Decimal | None
    estimated: Decimal | None = None
    confirmed: Decimal | None = None
    confirmed_at: datetime | None = None


@dataclass(frozen=True)
class AdmissionTotals:
    """Observed consumption and unresolved holds before a candidate reservation."""

    confirmed_current_24h: Decimal
    unresolved_holds: Decimal
    indeterminate_hold_count: int

    @property
    def committed_total(self) -> Decimal:
        return self.confirmed_current_24h + self.unresolved_holds


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    reason: Literal["allowed", "budget_exhausted", "unresolved_cost_amount_missing"]
    totals: AdmissionTotals
    candidate_reserve: Decimal


def ledger_costs(entries: Iterable[StudioCostLedger]) -> list[ReservationCost]:
    """Project persisted ledger entries into the conservative admission input."""
    rows: list[ReservationCost] = []
    for entry in entries:
        rows.append(
            ReservationCost(
                created_at=entry.created_at,
                cost_state=str(entry.cost_state),
                reserved=entry.reserved_usd,
                estimated=entry.estimated_usd,
                confirmed=entry.confirmed_usd,
                confirmed_at=entry.confirmed_at,
            )
        )
    return rows


def legacy_reservation_costs(entries: Iterable[StudioReservation]) -> list[ReservationCost]:
    """Project pre-ledger reservations without silently releasing their holds.

    Legacy confirmed rows lack an observed confirmation timestamp, so they are
    deliberately indeterminate at admission rather than attributed to their
    reservation time or treated as zero.
    """
    return [
        ReservationCost(
            created_at=entry.created_at,
            cost_state=str(entry.cost_state),
            reserved=entry.reserved_usd,
            estimated=entry.estimated_usd,
            confirmed=entry.confirmed_usd,
        )
        for entry in entries
    ]


def whole_run_reserve(
    *,
    mode: Literal["fixture", "recorded", "planning", "local", "live"],
    profile: StudioCostProfile | None,
    allowed_plan_revisions: int = 0,
    asset_retries: int = 0,
    planning_calls: Sequence[tuple[int, str]] = (),
) -> Decimal:
    """Return a candidate reservation, rejecting unconfigured live cost profiles."""
    if mode in {"fixture", "recorded", "planning", "local"}:
        # Planning review cannot submit media. Native text usage is recorded
        # separately; a zero media reserve does not claim that text is free.
        return ZERO
    if profile is None:
        raise CostConfigurationError("live_cost_profile_missing")
    return profile.reserve_with_revisions(
        allowed_plan_revisions,
        asset_retries=asset_retries,
        planning_calls=planning_calls,
    )


def stage_reservations(
    profile: StudioCostProfile,
    *,
    allowed_plan_revisions: int,
    asset_retries: int = 0,
    planning_calls: Sequence[tuple[int, str]] = (),
) -> tuple[tuple[str, str, Decimal], ...]:
    """Return deterministic ledger rows for the entire pre-reserved work scope."""
    _require_profile(profile)
    if allowed_plan_revisions < 0:
        raise CostConfigurationError("allowed_plan_revisions_must_not_be_negative")
    _validate_asset_retries(asset_retries)
    _validate_planning_calls(planning_calls, maximum_revision=allowed_plan_revisions + 1)
    rows: list[tuple[str, str, Decimal]] = [
        ("input_moderation", "initial", profile.input_moderation),
        *(
            [("ideation", "initial", profile.ideation)]
            if not _roles_for_revision(planning_calls, 1)
            else []
        ),
        ("plan_moderation", "initial", profile.plan_moderation),
        *[("video_request", f"month:{month}", profile.video_request) for month in range(1, 13)],
        ("narration_batch", "initial", profile.narration_batch),
        ("render", "initial", profile.render),
        ("final_moderation", "initial", profile.final_moderation),
    ]
    for revision in range(1, allowed_plan_revisions + 1):
        rows.extend(
            (
                ("input_moderation", f"revision:{revision}", profile.input_moderation),
                ("plan_moderation", f"revision:{revision}", profile.plan_moderation),
            )
        )
        if not _roles_for_revision(planning_calls, revision + 1):
            rows.append(("ideation", f"revision:{revision}", profile.ideation))
    for revision, role in planning_calls:
        rows.append(("ideation", f"revision:{revision}:role:{role}", profile.ideation))
    for attempt in range(2, asset_retries + 2):
        rows.extend(
            ("video_request", f"month:{month}:attempt:{attempt}", profile.video_request)
            for month in range(1, 13)
        )
        rows.append(("narration_batch", f"attempt:{attempt}", profile.narration_batch))
    return tuple(rows)


async def record_whole_run_reservation(
    session: AsyncSession,
    *,
    run_id: UUID,
    visitor_id: str,
    ip_hash: str,
    profile: StudioCostProfile,
    allowed_plan_revisions: int,
    asset_retries: int = 0,
    planning_calls: Sequence[tuple[int, str]] = (),
) -> Decimal:
    """Add reservation and stage ledger rows inside the caller's transaction."""
    from myloware.storage.studio_models import StudioCostLedger, StudioReservation

    reserve = whole_run_reserve(
        mode="live",
        profile=profile,
        allowed_plan_revisions=allowed_plan_revisions,
        asset_retries=asset_retries,
        planning_calls=planning_calls,
    )
    session.add(
        StudioReservation(
            run_id=run_id,
            visitor_id=visitor_id,
            ip_hash=ip_hash,
            reserved_usd=reserve,
            cost_state="reserved",
            cost_profile_version=profile.version,
        )
    )
    session.add_all(
        StudioCostLedger(
            run_id=run_id,
            profile_version=profile.version,
            stage=stage,
            operation_key=operation_key,
            reserved_usd=amount,
            cost_state="reserved",
        )
        for stage, operation_key, amount in stage_reservations(
            profile,
            allowed_plan_revisions=allowed_plan_revisions,
            asset_retries=asset_retries,
            planning_calls=planning_calls,
        )
    )
    return reserve


def _validate_asset_retries(asset_retries: int) -> None:
    if not isinstance(asset_retries, int) or isinstance(asset_retries, bool) or asset_retries < 0:
        raise CostConfigurationError("asset_retries_must_be_a_nonnegative_integer")


def _roles_for_revision(
    planning_calls: Sequence[tuple[int, str]], revision: int
) -> tuple[str, ...]:
    return tuple(role for call_revision, role in planning_calls if call_revision == revision)


def _validate_planning_calls(
    planning_calls: Sequence[tuple[int, str]], *, maximum_revision: int | None = None
) -> None:
    if isinstance(planning_calls, (str, bytes)):
        raise CostConfigurationError("planning_calls_must_be_revision_role_pairs")
    for revision, role in planning_calls:
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or (maximum_revision is not None and revision > maximum_revision)
            or not isinstance(role, str)
            or not role
            or (
                role not in {"repair_1", "repair_2"}
                and any(character not in "abcdefghijklmnopqrstuvwxyz_" for character in role)
            )
        ):
            raise CostConfigurationError("planning_calls_must_be_revision_role_pairs")
    by_revision: dict[int, set[str]] = {}
    for revision, role in planning_calls:
        roles = by_revision.setdefault(revision, set())
        if role in roles:
            raise CostConfigurationError("planning_calls_must_have_distinct_roles")
        roles.add(role)


async def record_cost_receipt(
    session: AsyncSession,
    *,
    run_id: UUID,
    profile_version: str,
    stage: str,
    operation_key: str,
    cost_state: Literal["estimated", "confirmed", "unknown", "submission_unknown"],
    estimated: Decimal | None = None,
    confirmed: Decimal | None = None,
    confirmed_at: datetime | None = None,
    receipt_reference: str | None = None,
    reconciled_at: datetime | None = None,
) -> None:
    """Update one existing stage receipt without creating an unreserved effect."""
    from myloware.storage.studio_models import StudioCostLedger

    ledger = await session.scalar(
        select(StudioCostLedger).where(
            StudioCostLedger.run_id == run_id,
            StudioCostLedger.profile_version == profile_version,
            StudioCostLedger.stage == stage,
            StudioCostLedger.operation_key == operation_key,
        )
    )
    if ledger is None:
        raise CostConfigurationError("cost_ledger_entry_missing")
    if cost_state == "confirmed" and confirmed_at is None:
        raise CostConfigurationError("confirmed_at_required")
    ledger.cost_state = cost_state
    ledger.estimated_usd = estimated
    ledger.confirmed_usd = confirmed
    ledger.confirmed_at = confirmed_at if cost_state == "confirmed" else None
    ledger.receipt_reference = receipt_reference
    ledger.reconciled_at = reconciled_at


async def persistent_admission_paused(session: AsyncSession) -> bool:
    """Return the durable owner pause state; absent control means not paused."""
    from myloware.storage.studio_models import StudioAdmissionControl

    control = await session.get(StudioAdmissionControl, 1)
    return bool(control and control.paused)


async def set_persistent_admission_pause(
    session: AsyncSession, *, paused: bool, reason: str | None = None
) -> None:
    """Update the durable admission control in the caller's transaction."""
    from myloware.storage.studio_models import StudioAdmissionControl

    control = await session.get(StudioAdmissionControl, 1)
    if control is None:
        session.add(StudioAdmissionControl(id=1, paused=paused, reason=reason))
        return
    control.paused = paused
    control.reason = reason


def admission_totals(
    reservations: list[ReservationCost], *, current_24h_start: datetime
) -> AdmissionTotals:
    """Count current confirmed costs and all unresolved/unknown holds.

    Confirmed consumption is period-bound.  A reservation in any unresolved,
    unknown, or unrecognized state remains a hold regardless of its age.  A
    missing or non-positive unresolved amount is deliberately indeterminate,
    never treated as zero.
    """
    confirmed = ZERO
    holds = ZERO
    indeterminate = 0
    for reservation in reservations:
        if reservation.cost_state in EXCLUDED_COST_STATES:
            continue
        if reservation.cost_state == "confirmed":
            if reservation.confirmed_at is None:
                indeterminate += 1
            elif _as_utc(reservation.confirmed_at) >= _as_utc(current_24h_start):
                if reservation.confirmed is None or reservation.confirmed < ZERO:
                    indeterminate += 1
                else:
                    confirmed += reservation.confirmed
            continue
        # Any new or malformed state is treated as unresolved.  Adding a state
        # cannot silently release a hold until its reconciliation rules exist.
        hold = _conservative_hold(reservation)
        if hold is None:
            indeterminate += 1
        else:
            holds += hold
    return AdmissionTotals(
        confirmed_current_24h=confirmed,
        unresolved_holds=holds,
        indeterminate_hold_count=indeterminate,
    )


def decide_admission(
    *,
    reservations: list[ReservationCost],
    current_24h_start: datetime,
    global_period_budget: Decimal,
    candidate_reserve: Decimal,
) -> AdmissionDecision:
    """Decide a budget-only admission with no database or provider effects."""
    if global_period_budget <= ZERO:
        raise CostConfigurationError("global_period_budget_must_be_positive")
    if candidate_reserve <= ZERO:
        raise CostConfigurationError("candidate_reserve_must_be_positive")
    totals = admission_totals(reservations, current_24h_start=current_24h_start)
    if totals.indeterminate_hold_count:
        return AdmissionDecision(
            allowed=False,
            reason="unresolved_cost_amount_missing",
            totals=totals,
            candidate_reserve=candidate_reserve,
        )
    allowed = totals.committed_total + candidate_reserve <= global_period_budget
    return AdmissionDecision(
        allowed=allowed,
        reason="allowed" if allowed else "budget_exhausted",
        totals=totals,
        candidate_reserve=candidate_reserve,
    )


def _as_utc(value: datetime) -> datetime:
    """Compare legacy-naive database values as UTC with aware receipt times."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_profile(profile: StudioCostProfile) -> None:
    if not profile.version.strip():
        raise CostConfigurationError("live_cost_profile_version_missing")
    for stage, amount in (
        ("input_moderation", profile.input_moderation),
        ("ideation", profile.ideation),
        ("plan_moderation", profile.plan_moderation),
        ("video_request", profile.video_request),
        ("narration_batch", profile.narration_batch),
        ("render", profile.render),
        ("final_moderation", profile.final_moderation),
    ):
        if amount <= ZERO:
            raise CostConfigurationError(f"live_cost_profile_{stage}_must_be_positive")


def _conservative_hold(reservation: ReservationCost) -> Decimal | None:
    amounts = (
        value
        for value in (reservation.reserved, reservation.estimated, reservation.confirmed)
        if value is not None and value > ZERO
    )
    return max(amounts, default=None)
