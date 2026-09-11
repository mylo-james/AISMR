"""Tests for the owner-gated recorded-library seed command."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy.ext.asyncio import create_async_engine

from myloware.cli.main import cli
from myloware.storage.models import Base
from myloware.studio.seed import RecordedSeedError, inspect_recorded_seed


class _Archive:
    def __init__(self, _root: Path) -> None:
        pass

    def all_media(self) -> tuple[()]:
        return ()


def _fake_archive(monkeypatch) -> None:
    monkeypatch.setattr("myloware.studio.seed.RecordedMediaArchive", _Archive)


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    archive = tmp_path / "fake-recorded-archive"
    archive.mkdir()
    (archive / "fixture-manifest.json").write_text('{"entries":[{"fake":true}]}')
    final = archive / "final.mp4"
    final.write_bytes(b"fake final media")
    return archive, final


def _receipt(archive: Path, final: Path) -> dict[str, object]:
    final_hash = sha256(final.read_bytes()).hexdigest()
    expiry = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    return {
        "schema_version": 1,
        "authorization_id": "test-owner-receipt",
        "archive_root": str(archive.resolve()),
        "manifest_sha256": sha256((archive / "fixture-manifest.json").read_bytes()).hexdigest(),
        "final_sha256": final_hash,
        "public_suitability": {
            "status": "passed",
            "receipt": "test-suitability",
            "receipt_id": "test-suitability-v1",
            "final_sha256": final_hash,
            "expires_at": expiry,
        },
        "public_rights": {
            "status": "passed",
            "receipt": "test-rights",
            "receipt_id": "test-rights-v1",
            "profile_version": "test-rights-profile-v1",
            "final_sha256": final_hash,
            "expires_at": expiry,
        },
    }


def test_seed_parser_requires_exact_owner_receipt(monkeypatch, tmp_path: Path) -> None:
    _fake_archive(monkeypatch)
    archive, final = _paths(tmp_path)
    with pytest.raises(RecordedSeedError, match="authorization receipt is required"):
        inspect_recorded_seed(archive_root=archive, final=final)


def test_seed_parser_rejects_mismatched_archive_or_final(monkeypatch, tmp_path: Path) -> None:
    _fake_archive(monkeypatch)
    archive, final = _paths(tmp_path)
    receipt = _receipt(archive, final)
    receipt["final_sha256"] = "0" * 64
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(RecordedSeedError, match="final_sha256 does not match"):
        inspect_recorded_seed(archive_root=archive, final=final, authorization_receipt=receipt_path)


def test_seed_cli_is_dry_run_by_default_and_does_not_build_library(
    monkeypatch, tmp_path: Path
) -> None:
    _fake_archive(monkeypatch)
    archive, final = _paths(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt(archive, final)))
    monkeypatch.setattr(
        "myloware.cli.studio.PortfolioLibraryService",
        lambda *_args, **_kwargs: pytest.fail("dry run must not initialize the library"),
    )

    result = CliRunner().invoke(
        cli,
        [
            "studio",
            "seed-recorded",
            "--archive-root",
            str(archive),
            "--final",
            str(final),
            "--authorization-receipt",
            str(receipt_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["final_sha256"] == sha256(final.read_bytes()).hexdigest()


def test_seed_cli_rejects_apply_without_separate_library(monkeypatch, tmp_path: Path) -> None:
    _fake_archive(monkeypatch)
    archive, final = _paths(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt(archive, final)))

    result = CliRunner().invoke(
        cli,
        [
            "studio",
            "seed-recorded",
            "--archive-root",
            str(archive),
            "--final",
            str(final),
            "--authorization-receipt",
            str(receipt_path),
            "--apply",
        ],
    )

    assert result.exit_code == 1
    assert "separate source/library database" in result.output


def test_seed_cli_apply_projects_only_fake_archive_media(monkeypatch, tmp_path: Path) -> None:
    _fake_archive(monkeypatch)
    archive, final = _paths(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt(archive, final)))
    library_root = tmp_path / "separate-library-media"
    library_db = tmp_path / "separate-library.db"

    result = CliRunner().invoke(
        cli,
        [
            "studio",
            "seed-recorded",
            "--archive-root",
            str(archive),
            "--final",
            str(final),
            "--authorization-receipt",
            str(receipt_path),
            "--library-database-url",
            f"sqlite+aiosqlite:///{library_db}",
            "--library-media-root",
            str(library_root),
            "--source-database-url",
            f"sqlite+aiosqlite:///{tmp_path / 'source.db'}",
            "--apply",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False and payload["library_entry_id"]
    assert list(library_root.glob("*.mp4"))
    assert final.read_bytes() == b"fake final media"


def test_admission_operator_commands_use_only_temporary_database(tmp_path: Path) -> None:
    database = tmp_path / "operator.db"
    url = f"sqlite+aiosqlite:///{database}"

    async def setup() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    import asyncio

    asyncio.run(setup())
    runner = CliRunner()
    assert runner.invoke(cli, ["studio", "admission-status", "--database-url", url]).exit_code == 0
    paused = runner.invoke(
        cli, ["studio", "pause-admissions", "--database-url", url, "--reason", "reconcile"]
    )
    assert paused.exit_code == 0 and json.loads(paused.output)["paused"] is True
    resumed = runner.invoke(cli, ["studio", "resume-admissions", "--database-url", url])
    assert resumed.exit_code == 0 and json.loads(resumed.output)["paused"] is False
