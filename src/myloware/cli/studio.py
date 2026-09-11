"""Owner-gated AISMR Studio maintenance commands."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

import click
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from myloware.storage.studio_models import StudioCostLedger
from myloware.storage.studio_store import acquire_admission_lock
from myloware.studio.budget import (
    persistent_admission_paused,
    record_cost_receipt,
    set_persistent_admission_pause,
)
from myloware.studio.portfolio_library_service import PortfolioLibraryService
from myloware.studio.seed import RecordedSeedError, apply_recorded_seed, inspect_recorded_seed


@click.group("studio")
def studio_group() -> None:
    """Inspect or explicitly maintain the AISMR Studio."""


def _run_database(url: str, operation):
    async def runner():
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            return await operation(factory)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


@studio_group.command("admission-status")
@click.option("--database-url", required=True)
def admission_status(database_url: str) -> None:
    """Read the durable owner admission pause state."""

    async def operation(factory):
        async with factory() as session:
            return await persistent_admission_paused(session)

    try:
        click.echo(json.dumps({"paused": _run_database(database_url, operation)}))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None


def _set_admission_pause(database_url: str, paused: bool, reason: str | None) -> None:
    async def operation(factory):
        async with factory() as session, session.begin():
            await acquire_admission_lock(session)
            await set_persistent_admission_pause(session, paused=paused, reason=reason)

    try:
        _run_database(database_url, operation)
        click.echo(json.dumps({"paused": paused, "reason": reason}))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None


@studio_group.command("pause-admissions")
@click.option("--database-url", required=True)
@click.option("--reason", required=True)
def pause_admissions(database_url: str, reason: str) -> None:
    """Persistently pause new Studio admissions with an operator reason."""
    _set_admission_pause(database_url, True, reason)


@studio_group.command("resume-admissions")
@click.option("--database-url", required=True)
def resume_admissions(database_url: str) -> None:
    """Clear the durable Studio admission pause after operator review."""
    _set_admission_pause(database_url, False, None)


@studio_group.command("reconcile-cost")
@click.option("--database-url", required=True)
@click.option("--ledger-id", required=True)
@click.option("--state", type=click.Choice(["confirmed", "estimated", "unknown"]), required=True)
@click.option("--amount", required=True)
@click.option("--receipt-reference", required=True)
@click.option("--confirmed-at", required=True)
def reconcile_cost(
    database_url: str,
    ledger_id: str,
    state: str,
    amount: str,
    receipt_reference: str,
    confirmed_at: str,
) -> None:
    """Record one supplied receipt without releasing an unreserved hold."""
    try:
        value = Decimal(amount)
        observed_at = datetime.fromisoformat(confirmed_at).astimezone(UTC).replace(tzinfo=None)
    except (InvalidOperation, ValueError) as exc:
        raise click.ClickException("amount and confirmed-at must be valid") from exc
    if value < 0 or not receipt_reference.strip():
        raise click.ClickException("amount must be nonnegative and receipt-reference is required")

    async def operation(factory):
        async with factory() as session, session.begin():
            await acquire_admission_lock(session)
            ledger = await session.get(StudioCostLedger, ledger_id)
            if ledger is None:
                raise ValueError("cost ledger entry is missing")
            if value > ledger.reserved_usd:
                raise ValueError("reconciled amount exceeds the reserved amount")
            await record_cost_receipt(
                session,
                run_id=ledger.run_id,
                profile_version=ledger.profile_version,
                stage=ledger.stage,
                operation_key=ledger.operation_key,
                cost_state=state,
                estimated=value if state == "estimated" else None,
                confirmed=value if state == "confirmed" else None,
                confirmed_at=observed_at if state == "confirmed" else None,
                receipt_reference=receipt_reference,
                reconciled_at=datetime.now(UTC).replace(tzinfo=None),
            )

    try:
        _run_database(database_url, operation)
        click.echo(json.dumps({"ledger_id": ledger_id, "state": state, "amount": str(value)}))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None


@studio_group.command("revoke-entry")
@click.option("--library-database-url", required=True)
@click.option(
    "--library-media-root", type=click.Path(path_type=Path, file_okay=False), required=True
)
@click.option("--protected-root", type=click.Path(path_type=Path), required=True)
@click.option("--entry-id", required=True)
@click.option("--reason", required=True)
def revoke_entry(
    library_database_url: str,
    library_media_root: Path,
    protected_root: Path,
    entry_id: str,
    reason: str,
) -> None:
    """Retire one public entry using an explicit separate library store."""
    try:
        identifier = UUID(entry_id)
    except ValueError as exc:
        raise click.ClickException("entry-id must be a UUID") from exc

    async def operation():
        engine = create_async_engine(library_database_url)
        service = PortfolioLibraryService(
            async_sessionmaker(engine, expire_on_commit=False),
            media_root=library_media_root,
            protected_roots=(protected_root,),
        )
        try:
            await service.revoke(identifier, reason=reason)
        finally:
            await engine.dispose()

    try:
        asyncio.run(operation())
        click.echo(json.dumps({"entry_id": entry_id, "revoked": True}))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None


@studio_group.command("seed-recorded")
@click.option(
    "--archive-root", type=click.Path(path_type=Path, exists=True, file_okay=False), required=True
)
@click.option(
    "--final",
    "final_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
)
@click.option(
    "--authorization-receipt", type=click.Path(path_type=Path, exists=True, dir_okay=False)
)
@click.option("--library-database-url")
@click.option("--library-media-root", type=click.Path(path_type=Path, file_okay=False))
@click.option("--source-database-url")
@click.option("--item-label", default="Recorded example", show_default=True)
@click.option(
    "--apply", "apply_seed", is_flag=True, help="Copy the verified final after all checks pass."
)
def seed_recorded(
    archive_root: Path,
    final_path: Path,
    authorization_receipt: Path | None,
    library_database_url: str | None,
    library_media_root: Path | None,
    source_database_url: str | None,
    item_label: str,
    apply_seed: bool,
) -> None:
    """Validate one recorded seed; dry run is the default and changes nothing."""
    try:
        seed = inspect_recorded_seed(
            archive_root=archive_root,
            final=final_path,
            authorization_receipt=authorization_receipt,
        )
        payload = {
            "mode": "recorded",
            "dry_run": not apply_seed,
            "archive_root": str(seed.archive_root),
            "manifest_sha256": seed.manifest_sha256,
            "final_sha256": seed.final_sha256,
            "authorization_id": seed.authorization_id,
        }
        if not apply_seed:
            click.echo(json.dumps(payload, sort_keys=True))
            return
        if not library_database_url or library_media_root is None or not source_database_url:
            raise RecordedSeedError(
                "--apply requires separate source/library database URLs and a library media root"
            )
        if _normalized_database_url(library_database_url) == _normalized_database_url(
            source_database_url
        ):
            raise RecordedSeedError("library database must be separate from the source database")
        service = PortfolioLibraryService(
            async_sessionmaker(create_async_engine(library_database_url), expire_on_commit=False),
            media_root=library_media_root,
            protected_roots=(seed.archive_root,),
        )
        payload["library_entry_id"] = asyncio.run(
            apply_recorded_seed(seed, library=service, item_label=item_label)
        )
        payload["dry_run"] = False
        click.echo(json.dumps(payload, sort_keys=True))
    except (RecordedSeedError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None


def _normalized_database_url(url: str) -> str:
    return url.replace("+aiosqlite", "").replace("+asyncpg", "")


def register(cli: click.Group) -> None:
    cli.add_command(studio_group)
