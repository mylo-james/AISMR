"""Carry the original queue claim through monthly stage transactions."""

from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from myloware.storage.models import Job, _utc_now
from myloware.storage.repositories import StaleJobClaim


@dataclass(frozen=True)
class JobClaim:
    job_id: UUID
    worker_id: str
    generation: int


current_job_claim: ContextVar[JobClaim | None] = ContextVar("monthly_job_claim", default=None)


async def require_current_claim(session: AsyncSession) -> None:
    """Fence stage writes using the queue owner's immutable claim, when running.

    The row lock is held only for the short stage transaction. A remote effect
    additionally needs its own persisted intent, so a reclaimed worker cannot
    repeat an accepted request after the original worker's lease was lost.
    """
    claim = current_job_claim.get()
    if claim is None:
        return  # Visitor HTTP decisions have their own cookie/receipt authority.
    row = await session.scalar(
        select(Job.id)
        .where(
            Job.id == claim.job_id,
            Job.claimed_by == claim.worker_id,
            Job.claim_generation == claim.generation,
            Job.status == "running",
            Job.lease_expires_at > _utc_now(),
        )
        .with_for_update()
    )
    if row is None:
        raise StaleJobClaim("monthly stage no longer owns its worker claim")
