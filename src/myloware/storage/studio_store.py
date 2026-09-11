"""Transactional visitor authority, admission, decisions and observable studio state."""

from __future__ import annotations

import hmac
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from myloware.config.studio import StudioSettings
from myloware.storage.models import Job, Run, _utc_now
from myloware.storage.repositories import JobRepository
from myloware.storage.studio_models import (
    StudioAdmissionLock,
    StudioAsset,
    StudioCostLedger,
    StudioDecision,
    StudioEvent,
    StudioPlannerRun,
    StudioReservation,
    StudioRun,
    StudioVisitor,
)
from myloware.studio.execution_profile import build_execution_profile, require_plan_execution
from myloware.studio.telemetry import project_telemetry, public_events
from myloware.workflows.monthly import resolve_item
from myloware.workflows.scenes import StudioPlan, parse_plan

TERMINAL = {
    "plan_complete",
    "video_complete",
    "published",
    "simulated_complete",
    "cancelled",
    "blocked",
    "failed",
}


class StudioError(ValueError):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


def digest(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def key_digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def request_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 64
        or not all(c.isalnum() or c in "_-" for c in value)
    ):
        raise StudioError("invalid_request_key", 422)
    return value


async def acquire_admission_lock(session: AsyncSession) -> None:
    """Acquire the database lock shared by admission and operator mutations."""
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    await session.execute(
        insert(StudioAdmissionLock)
        .values(id=1, generation=0)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await session.execute(
        update(StudioAdmissionLock)
        .where(StudioAdmissionLock.id == 1)
        .values(generation=StudioAdmissionLock.generation + 1)
    )


class StudioStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession], config: StudioSettings):
        self.factory = factory
        self.config = config

    def sign(self, purpose: str, value: str) -> str:
        return hmac.new(
            self.config.session_secret.get_secret_value().encode(),
            f"{purpose}:{value}".encode(),
            "sha256",
        ).hexdigest()

    def csrf(self, visitor_id: str) -> str:
        return self.sign("csrf", visitor_id)

    def share_token(self, run_id: UUID) -> str:
        return self.sign("read", str(run_id))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Serialize short decisions in the database, not an in-process mutex.

        Acquiring the write lock is the first statement, before any reads. This
        avoids SQLite read-to-write snapshot upgrade races. PostgreSQL locks the
        same row until commit. No network calls occur inside this context.
        """
        async with self.factory() as session, session.begin():
            await acquire_admission_lock(session)
            from myloware.workers.claims import require_current_claim

            await require_current_claim(session)
            yield session

    async def new_visitor(self, ip: str) -> tuple[StudioVisitor, str]:
        now = _utc_now()
        secret = secrets.token_urlsafe(32)
        visitor = StudioVisitor(
            id=uuid4().hex,
            secret_hash=key_digest(secret),
            ip_hash=self.sign("ip", ip),
            expires_at=now + timedelta(hours=self.config.session_hours),
        )
        async with self.transaction() as session:
            session.add(visitor)
            await session.flush()
        raw = f"{visitor.id}.{secret}"
        return visitor, f"{raw}.{self.sign('session', raw)}"

    async def visitor(self, cookie: str | None) -> StudioVisitor:
        try:
            visitor_id, secret, signature = (cookie or "").split(".")
        except ValueError as exc:
            raise StudioError("session_required", 401) from exc
        if len(visitor_id) != 32 or not hmac.compare_digest(
            signature, self.sign("session", f"{visitor_id}.{secret}")
        ):
            raise StudioError("session_required", 401)
        async with self.factory() as session:
            visitor = await session.get(StudioVisitor, visitor_id)
            if (
                visitor is None
                or visitor.expires_at <= _utc_now()
                or not hmac.compare_digest(str(visitor.secret_hash), key_digest(secret))
            ):
                raise StudioError("session_expired", 401)
            return visitor

    async def latest_run(self, visitor_id: str) -> str | None:
        async with self.factory() as session:
            run_id = await session.scalar(
                select(StudioRun.run_id)
                .where(StudioRun.visitor_id == visitor_id)
                .order_by(StudioRun.created_at.desc())
                .limit(1)
            )
            return str(run_id) if run_id else None

    async def admit(self, *, visitor_id: str, item_text: str, start_key: str, ip: str) -> UUID:
        """Reserve the entire bounded workflow before any model/media effect."""
        request_key(start_key)
        if self.config.mode == "planning" and self.config.planning_configuration_errors():
            raise StudioError("planning_runtime_not_ready", 503)
        if self.config.mode == "local" and self.config.local_configuration_errors():
            raise StudioError("local_runtime_not_ready", 503)
        if self.config.mode == "recorded" and item_text.strip().lower() != "teacup":
            raise StudioError("recorded_item_required", 422)
        item = resolve_item(item_text)
        now = _utc_now()
        cutoff = now - timedelta(hours=24)
        ip_hash = self.sign("ip", ip)
        unlimited_fixture = (
            self.config.mode == "fixture" and self.config.fixture_unlimited_admissions
        )
        async with self.transaction() as session:
            visitor = await session.get(StudioVisitor, visitor_id)
            if visitor is None or visitor.expires_at <= now:
                raise StudioError("session_expired", 401)
            previous = await session.scalar(
                select(StudioRun).where(
                    StudioRun.visitor_id == visitor_id, StudioRun.start_key == start_key
                )
            )
            if previous:
                # Random is resolved once; retries reuse the selected item.
                if item_text.lower() != "random" and previous.item_id != item.item_id:
                    raise StudioError("idempotency_conflict")
                return previous.run_id
            if not unlimited_fixture:
                count = await session.scalar(
                    select(func.count())
                    .select_from(StudioReservation)
                    .where(
                        StudioReservation.visitor_id == visitor_id,
                        StudioReservation.created_at >= cutoff,
                        StudioReservation.cost_state != "input_rejected",
                    )
                )
                if (count or 0) >= self.config.visitor_runs_24h:
                    raise StudioError("visitor_allowance_exhausted", 429)
                count = await session.scalar(
                    select(func.count())
                    .select_from(StudioReservation)
                    .where(
                        StudioReservation.ip_hash == ip_hash,
                        StudioReservation.created_at >= cutoff,
                        StudioReservation.cost_state != "input_rejected",
                    )
                )
                if (count or 0) >= self.config.ip_runs_24h:
                    raise StudioError("network_allowance_exhausted", 429)
                active_predicate = (
                    (StudioRun.mode == "local") & StudioRun.status.not_in(TERMINAL)
                    if self.config.mode == "local"
                    else (
                        StudioRun.status == "ideating"
                        if self.config.mode == "planning"
                        else StudioRun.status.not_in(TERMINAL)
                    )
                )
                if self.config.recorded_final_reuse:
                    # Review pauses have no scheduled worker. Expired hosted
                    # walkthroughs cannot reserve capacity indefinitely; retain
                    # their history and leave other execution profiles alone.
                    expired_recorded = (
                        (StudioRun.mode == "recorded")
                        & (StudioRun.expires_at <= now)
                        & StudioRun.run_id.in_(
                            select(StudioPlannerRun.run_id).where(
                                StudioPlannerRun.version == "recorded-v1"
                            )
                        )
                    )
                    active_predicate = active_predicate & ~expired_recorded
                active = await session.scalar(
                    select(func.count()).select_from(StudioRun).where(active_predicate)
                )
                if (active or 0) >= self.config.active_runs:
                    raise StudioError("studio_busy", 429)
            from myloware.studio.budget import (
                CostConfigurationError,
                decide_admission,
                ledger_costs,
                legacy_reservation_costs,
                persistent_admission_paused,
                record_whole_run_reservation,
                whole_run_reserve,
            )

            if self.config.admission_paused or await persistent_admission_paused(session):
                raise StudioError("admissions_paused", 429)
            from myloware.studio.planning_store import (
                planning_contract,
                planning_deadline_policy,
                purge_expired_planning_data,
            )

            await purge_expired_planning_data(
                session,
                now=now,
                preserve_artifacts=self.config.preserve_run_artifacts,
            )
            profile = self.config.cost_profile()
            local_media_receipt = None
            if self.config.mode == "local":
                from myloware.studio.local_scene_media import LocalSceneMediaArchive

                try:
                    local_media_receipt = LocalSceneMediaArchive(
                        self.config.local_media_root
                    ).receipt()
                except Exception as exc:
                    raise StudioError("local_media_archive_invalid", 503) from exc
            try:
                reservation = whole_run_reserve(
                    mode=self.config.mode,
                    profile=profile,
                    allowed_plan_revisions=self.config.plan_revisions,
                    asset_retries=self.config.asset_retries,
                    planning_calls=self.config.planning_calls(),
                )
            except CostConfigurationError as exc:
                raise StudioError("live_cost_profile_missing", 503) from exc
            if self.config.mode == "live":
                ledger = (await session.scalars(select(StudioCostLedger))).all()
                ledger_run_ids = {entry.run_id for entry in ledger}
                legacy_reservations = [
                    entry
                    for entry in (await session.scalars(select(StudioReservation))).all()
                    if entry.run_id not in ledger_run_ids
                ]
                decision = decide_admission(
                    reservations=[
                        *ledger_costs(ledger),
                        *legacy_reservation_costs(legacy_reservations),
                    ],
                    current_24h_start=cutoff,
                    global_period_budget=self.config.daily_budget_usd,
                    candidate_reserve=reservation,
                )
                if not decision.allowed:
                    raise StudioError(decision.reason, 429)
            run_id = uuid4()
            session.add(
                Run(
                    id=run_id,
                    workflow_name="monthly",
                    input=item.label,
                    user_id=visitor_id,
                    status="pending",
                    public_demo=False,
                )
            )
            await session.flush()
            run = StudioRun(
                run_id=run_id,
                visitor_id=visitor_id,
                start_key=start_key,
                read_token_hash=key_digest(self.share_token(run_id)),
                item_id=item.item_id,
                item_text=item.label,
                mode=self.config.mode,
                execution_profile=(
                    build_execution_profile() if self.config.mode != "recorded" else None
                ),
                status="ideating",
                expires_at=now + timedelta(hours=self.config.run_deadline_hours),
            )
            session.add(run)
            await session.flush()
            if self.config.recorded_final_reuse:
                from myloware.studio.hosted_recorded import recorded_bundle

                recorded_bundle(self.config)
                session.add(
                    StudioPlannerRun(
                        run_id=run_id,
                        version="recorded-v1",
                        configuration={
                            "recorded_final_reuse": True,
                            "bundle_sha256": self.config.recorded_bundle_sha256,
                            "plan_revisions": 0,
                        },
                        expires_at=now + timedelta(days=self.config.retention_days),
                    )
                )
            if self.config.mode != "recorded":
                session.add(
                    StudioPlannerRun(
                        run_id=run_id,
                        version=self.config.planner_version,
                        configuration={
                            "backend": self.config.ideation_backend,
                            "model": (
                                self.config.codex_model
                                if self.config.ideation_backend == "codex"
                                else self.config.openai_ideation_model
                            ),
                            "deadline_seconds": self.config.ideation_deadline_seconds,
                            "creative_workflow_deadline_seconds": self.config.creative_workflow_deadline_seconds,
                            "deadline_policy_sha256": digest(
                                planning_deadline_policy(
                                    deadline_seconds=self.config.ideation_deadline_seconds,
                                    creative_workflow_deadline_seconds=self.config.creative_workflow_deadline_seconds,
                                )
                            ),
                            "cost_profile_version": profile.version if profile else None,
                            "plan_revisions": self.config.plan_revisions,
                            "repair_attempts": self.config.creative_repair_attempts,
                            "contract_sha256": digest(
                                planning_contract(
                                    repair_attempts=self.config.creative_repair_attempts
                                )
                            ),
                            **(
                                {"local_media_receipt": local_media_receipt}
                                if local_media_receipt is not None
                                else {}
                            ),
                        },
                        expires_at=now + timedelta(days=self.config.retention_days),
                    )
                )
            if self.config.mode == "live":
                if profile is None:
                    raise StudioError("live_cost_profile_missing", 503)
                await record_whole_run_reservation(
                    session,
                    run_id=run_id,
                    visitor_id=visitor_id,
                    ip_hash=ip_hash,
                    profile=profile,
                    allowed_plan_revisions=self.config.plan_revisions,
                    asset_retries=self.config.asset_retries,
                    planning_calls=self.config.planning_calls(),
                )
            else:
                session.add(
                    StudioReservation(
                        run_id=run_id,
                        visitor_id=visitor_id,
                        ip_hash=ip_hash,
                        reserved_usd=reservation,
                        cost_state="reserved",
                    )
                )
            await session.flush()
            await self.event(
                session,
                run,
                "ideating",
                "admitted",
                {"mode": self.config.mode, "item": item.label},
            )
            await JobRepository(session).enqueue_async(
                "studio.advance",
                run_id=run_id,
                idempotency_key=f"studio:{run_id}",
                max_attempts=100000,
            )
            return run_id

    async def event(
        self,
        session: AsyncSession,
        run: StudioRun,
        stage: str,
        event_type: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        run.event_sequence = int(run.event_sequence or 0) + 1
        run.updated_at = _utc_now()
        session.add(
            StudioEvent(
                run_id=run.run_id,
                sequence=run.event_sequence,
                stage=stage,
                event_type=event_type,
                detail=detail or {},
            )
        )

    async def get_run(self, run_id: UUID) -> StudioRun:
        async with self.factory() as session:
            run = await session.get(StudioRun, run_id)
            if run is None:
                raise StudioError("run_not_found", 404)
            return run

    async def save_plan(self, plan: StudioPlan, moderation: dict[str, Any]) -> None:
        async with self.transaction() as session:
            run = await session.get(StudioRun, plan.run_id)
            if (
                run is None
                or run.cancelled
                or run.status != "ideating"
                or run.revision != plan.revision
            ):
                return
            if run.item_id != plan.item_id or run.item_text != plan.item_text:
                raise StudioError("plan_item_mismatch")
            try:
                require_plan_execution(plan, run.execution_profile)
            except ValueError as exc:
                raise StudioError("plan_execution_version_mismatch") from exc
            run.plan = plan.model_dump(mode="json")
            run.plan_hash = plan.canonical_sha256
            run.moderation = moderation
            run.status = "idea_review"
            run.plan_review_created_at = _utc_now()
            run.plan_review_expires_at = run.plan_review_created_at + timedelta(
                hours=self.config.plan_review_hours
            )
            await self.event(
                session,
                run,
                "idea_review",
                "plan_ready",
                {"revision": run.revision, "scenes": 12},
            )

    async def decide(
        self,
        *,
        run_id: UUID,
        visitor_id: str,
        gate: str,
        revision: int,
        subject_hash: str,
        decision: str,
        decision_key: str,
        publish_to_gallery: bool = False,
        visibility_version: str | None = None,
        revision_feedback: dict[str, Any] | None = None,
    ) -> None:
        request_key(decision_key)
        feedback = None
        if revision_feedback is not None:
            if gate != "ideas" or decision != "revise":
                raise StudioError("invalid_revision_feedback", 422)
            from myloware.studio.planning_store import PlanRevisionRequest

            try:
                feedback = PlanRevisionRequest.model_validate(revision_feedback).model_dump()
            except ValueError as exc:
                raise StudioError("invalid_revision_feedback", 422) from exc
        if gate not in {"ideas", "final"} or decision not in {
            "approve",
            "cancel",
            "revise",
        }:
            raise StudioError("invalid_decision", 422)
        if publish_to_gallery and (
            gate != "final" or decision != "approve" or visibility_version != "recent-creations-v1"
        ):
            raise StudioError("invalid_gallery_consent", 422)
        now = _utc_now()
        async with self.transaction() as session:
            run = await session.get(StudioRun, run_id)
            if run is None or run.visitor_id != visitor_id:
                raise StudioError("run_not_found", 404)
            if run.mode == "planning" and (gate != "ideas" or publish_to_gallery):
                raise StudioError("planning_only", 409)
            visitor = await session.get(StudioVisitor, visitor_id)
            if visitor is None or visitor.expires_at <= now or run.expires_at <= now:
                raise StudioError("session_expired", 401)
            previous = await session.scalar(
                select(StudioDecision).where(
                    StudioDecision.run_id == run_id,
                    StudioDecision.request_key == decision_key,
                )
            )
            if previous:
                if (
                    previous.gate,
                    previous.revision,
                    previous.subject_hash,
                    previous.decision,
                ) != (gate, revision, subject_hash, decision):
                    raise StudioError("idempotency_conflict")
                if previous.payload.get("revision_feedback") != feedback and (
                    feedback is not None or previous.payload.get("revision_feedback") is not None
                ):
                    raise StudioError("idempotency_conflict")
                return
            expected_status = "idea_review" if gate == "ideas" else "final_review"
            expected_hash = run.plan_hash if gate == "ideas" else self.final_review_hash(run)
            if (
                run.cancelled
                or run.status != expected_status
                or run.revision != revision
                or not expected_hash
                or not hmac.compare_digest(subject_hash, str(expected_hash))
            ):
                raise StudioError("stale_review")
            review_expires_at = (
                run.final_review_expires_at if gate == "final" else run.plan_review_expires_at
            )
            if review_expires_at is not None and review_expires_at <= now:
                raise StudioError(f"{gate}_review_expired", 410)
            if gate == "ideas" and decision != "cancel":
                try:
                    plan = parse_plan(run.plan)
                    require_plan_execution(plan, run.execution_profile)
                except ValueError as exc:
                    raise StudioError("plan_execution_version_mismatch") from exc
                if plan.canonical_sha256 != run.plan_hash:
                    raise StudioError("plan_hash_mismatch")
            planner = await session.get(StudioPlannerRun, run_id)
            allowed_revisions = (
                int(planner.configuration["plan_revisions"])
                if planner
                else self.config.plan_revisions
            )
            if decision == "revise" and (
                gate != "ideas" or run.mode == "recorded" or run.revision > allowed_revisions
            ):
                raise StudioError("revision_allowance_exhausted")
            if decision == "revise" and planner is not None:
                from myloware.studio.planning_store import compatible_planning_contract

                if not compatible_planning_contract(planner.configuration):
                    raise StudioError("planner_version_unavailable")
            payload: dict[str, Any] = (
                {"artifact_hash": run.final_hash}
                if gate == "final"
                else {"plan_hash": run.plan_hash}
            )
            if decision == "revise":
                if feedback is not None and (planner is None or planner.version != "creative-v2"):
                    raise StudioError("targeted_revision_unavailable", 422)
                if planner is not None and planner.version == "creative-v2":
                    payload["previous_plan"] = run.plan
                    if feedback is not None:
                        payload["revision_feedback"] = feedback
            gallery_consent = None
            if publish_to_gallery:
                gallery_consent = self._gallery_consent(
                    run, decision_key, subject_hash, visibility_version
                )
                payload["gallery_consent"] = {
                    "visibility_version": visibility_version,
                    "subject_hash": gallery_consent.consent_subject_hash,
                }
            session.add(
                StudioDecision(
                    run_id=run_id,
                    visitor_id=visitor_id,
                    gate=gate,
                    revision=revision,
                    subject_hash=subject_hash,
                    request_key=decision_key,
                    decision=decision,
                    payload=payload or {},
                    expires_at=run.expires_at,
                )
            )
            if decision == "cancel":
                run.cancelled = True
                run.status = "cancelled"
                # Only fixture effects are known to be free; accepted live calls
                # retain their reservation until a real billing reconciliation.
                if run.mode in {"fixture", "recorded"}:
                    await self.close_fixture_reservation(session, run_id)
            elif decision == "revise":
                run.revision += 1
                run.plan = None
                run.plan_hash = None
                run.plan_approved_hash = None
                run.moderation = None
                run.status = "ideating"
            elif gate == "ideas":
                run.plan_approved_hash = subject_hash
                run.status = "plan_complete" if run.mode == "planning" else "generating"
            else:
                run.status = "video_complete"
                if gallery_consent is not None:
                    from myloware.studio.portfolio_library_service import (
                        record_gallery_projection_intent,
                    )

                    await record_gallery_projection_intent(session, consent=gallery_consent)
                if run.mode in {"fixture", "recorded"}:
                    await self.close_fixture_reservation(session, run_id)
            await self.event(
                session,
                run,
                run.status,
                (
                    "plan_completed"
                    if run.status == "plan_complete"
                    else "video_completed" if run.status == "video_complete" else "visitor_decision"
                ),
                {"gate": gate, "decision": decision, "revision": revision},
            )
            if run.status in {"video_complete", "cancelled"}:
                await self.queue_cleanup(session, run_id)
            await session.execute(
                update(Job)
                .where(
                    Job.job_type == "studio.advance",
                    Job.run_id == run_id,
                    Job.status == "pending",
                )
                .values(available_at=now)
            )

    @staticmethod
    def _gallery_consent(
        run: StudioRun,
        decision_key: str,
        final_review_subject_hash: str,
        visibility_version: str | None,
    ):
        """Build public projection facts only from exact verified final evidence."""
        from myloware.studio.portfolio_library_service import SourceGalleryConsent

        metadata = dict(run.final_metadata or {})
        suitability = metadata.get("public_suitability")
        rights = metadata.get("public_rights")
        if (
            run.mode not in {"live", "recorded"}
            or not run.final_hash
            or metadata.get("media_verified") is not True
            or metadata.get("sha256") != run.final_hash
            or not isinstance(suitability, dict)
            or suitability.get("status") != "passed"
            or suitability.get("final_hash") != run.final_hash
            or not isinstance(suitability.get("receipt"), str)
            or not isinstance(suitability.get("expires_at"), str)
            or not isinstance(rights, dict)
            or rights.get("status") != "passed"
            or rights.get("final_hash") != run.final_hash
            or not isinstance(rights.get("receipt"), str)
            or not isinstance(rights.get("profile_expires_at"), str)
            or not isinstance(rights.get("profile_version"), str)
            or not isinstance(rights.get("profile_receipt_id"), str)
        ):
            raise StudioError("gallery_eligibility_unverified", 422)
        consent_hash = digest(
            {
                "final_review_subject_hash": final_review_subject_hash,
                "visibility_version": visibility_version,
                "gallery": True,
            }
        )
        return SourceGalleryConsent(
            run_id=run.run_id,
            revision=run.revision,
            final_hash=run.final_hash,
            decision_key=decision_key,
            consent_subject_hash=consent_hash,
            source_mode=run.mode,
            item_label=run.item_text,
            public_suitability_receipt=suitability["receipt"],
            rights_receipt=rights["receipt"],
            history={
                "months": 12,
                "rights_profile_expires_at": rights.get("profile_expires_at"),
                "rights_profile_version": rights.get("profile_version"),
                "rights_profile_receipt_id": rights.get("profile_receipt_id"),
                "suitability_expires_at": suitability.get("expires_at"),
                "suitability_receipt_id": suitability.get("receipt"),
            },
        )

    @staticmethod
    def final_review_hash(run: StudioRun) -> str | None:
        metadata: dict[str, Any] = dict(run.final_metadata or {})
        if not run.final_hash or metadata.get("media_verified") is not True:
            return None
        return digest(
            {
                "run_id": str(run.run_id),
                "revision": run.revision,
                "artifact_hash": run.final_hash,
                "gate": "final",
            }
        )

    @staticmethod
    def publication_hash(run: StudioRun) -> str | None:
        if not run.final_hash or not run.publish_config:
            return None
        return digest(
            {
                "run_id": str(run.run_id),
                "revision": run.revision,
                "artifact_hash": run.final_hash,
                "publication": run.publish_config,
            }
        )

    async def close_fixture_reservation(self, session: AsyncSession, run_id: UUID) -> None:
        reservation = await session.get(StudioReservation, run_id)
        if reservation:
            reservation.confirmed_usd = Decimal(0)
            reservation.cost_state = "confirmed"
            reservation.closed_at = _utc_now()

    @staticmethod
    async def queue_cleanup(session: AsyncSession, run_id: UUID) -> None:
        await JobRepository(session).enqueue_async(
            "studio.cleanup",
            run_id=run_id,
            idempotency_key=f"studio:cleanup:{run_id}",
            max_attempts=5,
        )

    async def stop(self, run_id: UUID, code: str, *, status: str = "blocked") -> None:
        async with self.transaction() as session:
            run = await session.get(StudioRun, run_id)
            if run and run.status not in TERMINAL:
                run.status = status
                run.error_code = code
                await self.event(session, run, status, code)
                if run.mode in {"fixture", "recorded"}:
                    await self.close_fixture_reservation(session, run_id)
                if code == "input_moderation_blocked":
                    reservation = await session.get(StudioReservation, run_id)
                    if reservation:
                        reservation.cost_state = "input_rejected"
                if status in {"failed", "cancelled", "blocked"}:
                    await self.queue_cleanup(session, run_id)

    async def snapshot(
        self, run_id: UUID, *, visitor_id: str | None = None, share: str | None = None
    ) -> dict[str, Any]:
        async with self.factory() as session:
            run = await session.get(StudioRun, run_id)
            if run is None:
                raise StudioError("run_not_found", 404)
            owner = visitor_id is not None and run.visitor_id == visitor_id
            shared = share is not None and hmac.compare_digest(
                str(run.read_token_hash), key_digest(share)
            )
            if not owner and not shared:
                raise StudioError("run_not_found", 404)
            if shared and run.created_at + timedelta(days=self.config.retention_days) <= _utc_now():
                raise StudioError("share_expired", 410)
            events = (
                await session.scalars(
                    select(StudioEvent)
                    .where(StudioEvent.run_id == run_id)
                    .order_by(StudioEvent.sequence)
                )
            ).all()
            assets = (
                await session.scalars(
                    select(StudioAsset)
                    .where(
                        StudioAsset.run_id == run_id,
                        StudioAsset.revision == run.revision,
                    )
                    .order_by(StudioAsset.ordinal, StudioAsset.kind, StudioAsset.attempt)
                )
            ).all()
            reservation = await session.get(StudioReservation, run_id)
            planner = await session.get(StudioPlannerRun, run_id)
            if run.plan is not None:
                try:
                    saved_plan = parse_plan(run.plan)
                    require_plan_execution(saved_plan, run.execution_profile)
                except ValueError as exc:
                    raise StudioError("saved_plan_invalid") from exc
                if saved_plan.canonical_sha256 != run.plan_hash:
                    raise StudioError("plan_hash_mismatch")
            from myloware.studio.planning_store import compatible_planning_contract

            revision_compatible = planner is None or compatible_planning_contract(
                planner.configuration
            )
            preview = None
            final_metadata: dict[str, Any] = dict(run.final_metadata or {})
            eligibility = {"eligible": False, "reason": "final_unavailable"}
            if run.status == "final_review" and run.final_hash:
                try:
                    self._gallery_consent(
                        run,
                        "snapshot-eligibility",
                        self.final_review_hash(run) or "",
                        "recent-creations-v1",
                    )
                    eligibility = {"eligible": True, "reason": None}
                except StudioError as exc:
                    eligibility = {"eligible": False, "reason": exc.code}
            if (
                run.final_hash
                and final_metadata.get("media_verified") is True
                and final_metadata.get("retention") != "evicted"
            ):
                # All prepublication bytes are served by the scoped media API.
                access = "" if owner else f"?share={share}"
                preview = {
                    "url": f"/v1/studio/runs/{run_id}/preview{access}",
                    "sha256": run.final_hash,
                    "review_hash": self.final_review_hash(run),
                    "metadata": {
                        key: value
                        for key, value in (run.final_metadata or {}).items()
                        if key
                        in {
                            "duration_seconds",
                            "width",
                            "height",
                            "byte_size",
                            "media_verified",
                            "fixture",
                            "recorded",
                            "recorded_final_reuse",
                        }
                    },
                }
            return {
                "id": str(run_id),
                "mode": run.mode,
                "recorded_final_reuse": bool(
                    planner and planner.configuration.get("recorded_final_reuse")
                ),
                "planning_only": run.mode == "planning",
                "item": run.item_text,
                "status": run.status,
                "revision": run.revision,
                "planner_version": planner.version if planner else "single-v1",
                "can_revise": bool(
                    owner
                    and run.status == "idea_review"
                    and run.mode != "recorded"
                    and revision_compatible
                    and run.expires_at > _utc_now()
                    and run.revision
                    <= (
                        int(planner.configuration["plan_revisions"])
                        if planner
                        else self.config.plan_revisions
                    )
                ),
                "plan": run.plan,
                "plan_hash": run.plan_hash,
                "error_code": run.error_code,
                "review_expired": bool(
                    (
                        run.status == "idea_review"
                        and run.plan_review_expires_at
                        and run.plan_review_expires_at <= _utc_now()
                    )
                    or (
                        run.status == "final_review"
                        and run.final_review_expires_at
                        and run.final_review_expires_at <= _utc_now()
                    )
                ),
                "can_act": bool(
                    owner
                    and run.expires_at > _utc_now()
                    and run.status in {"idea_review", "final_review"}
                    and not (
                        (
                            run.status == "idea_review"
                            and run.plan_review_expires_at
                            and run.plan_review_expires_at <= _utc_now()
                        )
                        or (
                            run.status == "final_review"
                            and run.final_review_expires_at
                            and run.final_review_expires_at <= _utc_now()
                        )
                    )
                ),
                "expires_at": run.expires_at.isoformat() + "Z",
                "share_url": (
                    f"{self.config.origin}/?run={run_id}&share={self.share_token(run_id)}"
                    if owner
                    else None
                ),
                "events": public_events(events),
                "telemetry": project_telemetry(events=events, assets=assets, run=run),
                "assets": [
                    {
                        "ordinal": a.ordinal,
                        "kind": a.kind,
                        "attempt": a.attempt,
                        "status": a.status,
                        "queue_position": a.queue_position,
                    }
                    for a in assets
                ],
                "final": preview,
                "gallery_eligibility": eligibility,
                "private_review_expires_at": (
                    (run.final_review_expires_at or run.expires_at).isoformat() + "Z"
                    if run.status == "final_review"
                    else None
                ),
                "retention": final_metadata.get("retention"),
                "cost": (
                    {
                        "reserved": str(reservation.reserved_usd),
                        "estimated": (
                            str(reservation.estimated_usd)
                            if reservation.estimated_usd is not None
                            else None
                        ),
                        "confirmed": (
                            str(reservation.confirmed_usd)
                            if reservation.confirmed_usd is not None
                            else None
                        ),
                        "state": reservation.cost_state,
                    }
                    if reservation
                    else None
                ),
            }
