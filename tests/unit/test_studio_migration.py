"""Migration proof for the additive AISMR studio schema.

The legacy 001 migration is PostgreSQL-specific. This test starts with a
pre-studio SQLite baseline, stamps the already-existing 004 state, and runs
the additive migrations from 005 through the current head against a disposable database.
"""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import Column, DateTime, MetaData, String, Table, create_engine, inspect

from alembic import command


def test_additive_studio_migrations_upgrade_a_pre_studio_sqlite_database(
    tmp_path, monkeypatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'pre_studio.db'}"
    engine = create_engine(database_url)
    legacy = MetaData()
    Table(
        "runs",
        legacy,
        Column("id", String(36), primary_key=True),
        Column("created_at", DateTime()),
    )
    Table(
        "artifacts",
        legacy,
        Column("id", String(36), primary_key=True),
        Column("run_id", String(36)),
    )
    Table(
        "jobs",
        legacy,
        Column("id", String(36), primary_key=True),
        Column("job_type", String(80)),
    )
    legacy.create_all(engine)

    from myloware.config import settings

    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config("alembic.ini")
    command.stamp(config, "004_public_demo_runs")
    command.upgrade(config, "head")

    inspector = inspect(engine)
    assert {
        "studio_visitors",
        "studio_runs",
        "studio_reservations",
        "studio_decisions",
        "studio_assets",
        "studio_events",
    } <= set(inspector.get_table_names())
    assert "claim_generation" in {column["name"] for column in inspector.get_columns("jobs")}
    assert inspector.get_columns("jobs")[-1]["name"] == "claim_generation"
    assert {
        "studio_cost_ledger",
        "studio_admission_control",
        "studio_planner_runs",
        "studio_planning_operations",
    } <= set(inspector.get_table_names())
    assert {"cost_profile_version"} <= {
        column["name"] for column in inspector.get_columns("studio_reservations")
    }
    assert {"confirmed_at"} <= {
        column["name"] for column in inspector.get_columns("studio_cost_ledger")
    }
    assert {
        "execution_profile",
        "plan_review_created_at",
        "plan_review_expires_at",
        "final_review_created_at",
        "final_review_expires_at",
    } <= {column["name"] for column in inspector.get_columns("studio_runs")}
    engine.dispose()


def test_planning_migration_uses_postgres_uuid_foreign_keys() -> None:
    import importlib.util
    from pathlib import Path
    from unittest.mock import patch

    from sqlalchemy.dialects import postgresql

    path = Path("alembic/versions/010_studio_creative_planning.py")
    spec = importlib.util.spec_from_file_location("creative_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with (
        patch.object(migration.op, "create_table") as tables,
        patch.object(migration.op, "create_index"),
    ):
        migration.upgrade()
    for call in tables.call_args_list:
        for column in call.args[1:]:
            if column.name in {"run_id", "id"}:
                assert str(column.type.compile(dialect=postgresql.dialect())) == "UUID"
