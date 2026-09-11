"""Offline evidence for the deployment profile's native driver and CLI seams."""

from __future__ import annotations

import inspect
import ssl
from pathlib import Path

import pytest
from asyncpg import connect_utils
from click.testing import CliRunner
from dotenv import dotenv_values
from psycopg import pq
from sqlalchemy.engine import make_url

from myloware.cli.main import cli
from myloware.config.studio import StudioSettings


def test_public_template_stays_closed_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in dotenv_values("deploy/aismr-app.env.example").items():
        if value is not None:
            monkeypatch.setenv(key, value)
    config = StudioSettings(_env_file=None, OPENAI_API_KEY="", FAL_API_KEY="")
    assert config.origin == "https://aismr.mjames.dev"
    assert config.ideation_backend == "openai"
    assert config.cookie_secure and config.admission_paused
    assert not config.live_enabled
    assert {
        "live_effects_disabled",
        "fal_credentials_missing",
        "openai_credentials_missing",
    } <= set(config.live_configuration_errors())


@pytest.mark.parametrize("database", ["aismr_source", "aismr_library"])
def test_native_asyncpg_profile_requires_verified_tls(
    monkeypatch: pytest.MonkeyPatch, database: str
) -> None:
    """Use installed SQLAlchemy and asyncpg parsing, with no network connection."""
    ca_file = ssl.get_default_verify_paths().cafile
    assert ca_file and Path(ca_file).is_file(), "test environment needs its native CA bundle"
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    monkeypatch.setenv("PGSSLROOTCERT", ca_file)
    url = make_url(f"postgresql+asyncpg://aismr:test-only@db.example.invalid/{database}")
    dialect = url.get_dialect()()
    _, args = dialect.create_connect_args(url)
    # The installed driver's parser verifies the certificate policy used by connect().
    kwargs = dict.fromkeys(inspect.signature(connect_utils._parse_connect_dsn_and_args).parameters)
    kwargs.update(args)
    _, parameters = connect_utils._parse_connect_dsn_and_args(**kwargs)
    assert parameters.sslmode.name == "verify_full"
    assert parameters.ssl.check_hostname
    assert parameters.ssl.verify_mode == ssl.CERT_REQUIRED
    libpq_options = {option.keyword: option.envvar for option in pq.Conninfo.get_defaults()}
    assert libpq_options[b"sslmode"] == b"PGSSLMODE"
    assert libpq_options[b"sslrootcert"] == b"PGSSLROOTCERT"


def test_aismr_command_identifies_the_application() -> None:
    runner = CliRunner()
    version = runner.invoke(cli, ["--version"])
    help_result = runner.invoke(cli, ["--help"])
    assert version.exit_code == help_result.exit_code == 0
    assert version.output.startswith("aismr, version ")
    assert "AISMR: reviewed video creation with LangGraph" in help_result.output
