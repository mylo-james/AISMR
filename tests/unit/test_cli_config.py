"""Unit tests for config CLI commands."""

from __future__ import annotations

from click.testing import CliRunner

from myloware.cli.main import cli


def test_config_show_prints_table(monkeypatch) -> None:
    # Make output deterministic and avoid leaking local DB URLs.
    monkeypatch.setattr("myloware.cli.config.settings.llama_stack_url", "http://localhost:5000")
    monkeypatch.setattr("myloware.cli.config.settings.llama_stack_model", "m1")
    monkeypatch.setattr("myloware.cli.config.settings.project_id", "proj")
    monkeypatch.setattr("myloware.cli.config.settings.milvus_uri", None)
    monkeypatch.setattr("myloware.cli.config.settings.database_url", "sqlite:///test.db")
    monkeypatch.setattr("myloware.cli.config.settings.api_host", "127.0.0.1")
    monkeypatch.setattr("myloware.cli.config.settings.api_port", 8000)
    monkeypatch.setattr("myloware.cli.config.settings.environment", "test")

    result = CliRunner().invoke(cli, ["config", "show"])
    assert result.exit_code == 0
    assert "AISMR Configuration" in result.output
    assert "http://localhost:5000" in result.output
