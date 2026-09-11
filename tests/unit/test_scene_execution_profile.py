from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text

from myloware.studio.execution_profile import (
    build_execution_profile,
    require_plan_execution,
    validate_execution_profile,
)
from myloware.workflows.monthly import deterministic_fixture_plan
from myloware.workflows.scenes import deterministic_scene_plan, parse_plan


def test_pinned_profile_matches_shipped_preset_and_stays_calendar_free() -> None:
    profile = build_execution_profile()
    assert profile["pipeline_version"] == "scene-v2"
    assert profile["voice_profile"]["delivery_prefix"] == "[whispers]"
    assert (
        profile["render_preset_sha256"]
        == "814c8de9532f053160308a273cdfdaac3837053ecc91f150740eb9c1dd9704f6"
    )
    assert (
        profile["voice_profile_sha256"]
        == "99bb477d104635a8dae416d7e90a2ae35402a0ac19fa939b48f49674918e6d3a"
    )
    assert "january" not in json.dumps(profile).lower()


@pytest.mark.parametrize(
    ("field", "value"),
    [("voice", "other-voice"), ("stability", 0.8), ("delivery_prefix", "[shouts]")],
)
def test_profile_changes_after_admission_fail_digest_validation(field, value) -> None:
    profile = deepcopy(build_execution_profile())
    profile["voice_profile"][field] = value
    with pytest.raises(ValueError):
        validate_execution_profile(profile)


def test_plan_dispatch_does_not_silently_migrate_legacy_audio() -> None:
    legacy = deterministic_fixture_plan(uuid4(), "teacup")
    scene = deterministic_scene_plan(uuid4(), "teacup")
    profile = build_execution_profile()
    assert require_plan_execution(parse_plan(legacy.model_dump()), None) is None
    assert require_plan_execution(parse_plan(scene.model_dump()), profile) == profile
    for plan, execution in ((legacy, profile), (scene, None), (scene, {})):
        with pytest.raises(ValueError):
            require_plan_execution(plan, execution)


def test_nullable_migration_preserves_existing_plan_bytes_and_hash(tmp_path, monkeypatch) -> None:
    from alembic.config import Config
    from sqlalchemy import create_engine

    from alembic import command
    from myloware.config import settings

    database_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(database_url)
    plan = deterministic_fixture_plan(uuid4(), "teacup")
    stored = plan.model_dump_json()
    before_hash = sha256(stored.encode()).hexdigest()
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE studio_runs (run_id TEXT PRIMARY KEY, plan TEXT, plan_hash TEXT)")
        )
        connection.execute(
            text("INSERT INTO studio_runs VALUES (:id, :plan, :hash)"),
            {"id": str(plan.run_id), "plan": stored, "hash": plan.canonical_sha256},
        )
    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config("alembic.ini")
    command.stamp(config, "010_studio_creative_planning")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT plan, plan_hash, execution_profile FROM studio_runs")
        ).one()
        assert row.execution_profile is None
        assert sha256(row.plan.encode()).hexdigest() == before_hash
        assert parse_plan(row.plan).canonical_sha256 == row.plan_hash == plan.canonical_sha256
    engine.dispose()
