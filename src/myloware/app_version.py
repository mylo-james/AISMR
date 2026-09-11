"""Application version helper.

Use importlib.metadata when installed, and fall back to reading pyproject.toml
when running from source via PYTHONPATH (no installed dist metadata).
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

from myloware.paths import get_repo_root


def get_app_version(package_name: str = "aismr") -> str:
    try:
        return _dist_version(package_name)
    except PackageNotFoundError:
        repo_root = get_repo_root()
        pyproject = repo_root / "pyproject.toml"
        try:
            with pyproject.open("rb") as f:
                data = tomllib.load(f)
            return str(data.get("project", {}).get("version", "0.0.0"))
        except (OSError, tomllib.TOMLDecodeError, AttributeError, TypeError):
            return "0.0.0"
