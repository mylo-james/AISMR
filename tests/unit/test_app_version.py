"""Unit tests for app version helper."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError


def test_get_app_version_uses_dist_metadata() -> None:
    from myloware.app_version import get_app_version

    version = get_app_version("aismr")
    assert isinstance(version, str)
    assert version


def test_get_app_version_falls_back_to_pyproject(monkeypatch, tmp_path) -> None:
    from myloware import app_version as mod

    monkeypatch.setattr(
        mod, "_dist_version", lambda _name: (_ for _ in ()).throw(PackageNotFoundError())
    )
    monkeypatch.setattr(mod, "get_repo_root", lambda: tmp_path)
    (tmp_path / "pyproject.toml").write_bytes(b'[project]\nversion = "1.2.3"\n')

    assert mod.get_app_version("missing") == "1.2.3"


def test_get_app_version_returns_default_on_read_error(monkeypatch, tmp_path) -> None:
    from myloware import app_version as mod

    monkeypatch.setattr(
        mod, "_dist_version", lambda _name: (_ for _ in ()).throw(PackageNotFoundError())
    )
    monkeypatch.setattr(mod, "get_repo_root", lambda: tmp_path)
    (tmp_path / "pyproject.toml").write_text("not toml")

    assert mod.get_app_version("missing") == "0.0.0"
