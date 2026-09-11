"""Exercise the documented Make recipe in an isolated checkout directory."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_make_demo_run_preserves_existing_configuration(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    shutil.copy(root / "Makefile", tmp_path / "Makefile")
    shutil.copy(root / ".env.example", tmp_path / ".env.example")
    (tmp_path / "scripts").mkdir()
    shutil.copy(root / "scripts/configure_demo.py", tmp_path / "scripts/configure_demo.py")
    (tmp_path / ".env").write_text(
        "# existing configuration\nAPI_KEY=local-test-value\nDISABLE_BACKGROUND_WORKFLOWS=true\n"
    )
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}
    first = subprocess.run(
        ["make", "demo-run"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )
    assert first.returncode == 0, first.stdout + first.stderr
    configured = (tmp_path / ".env").read_text()
    assert "API_KEY=local-test-value\n" in configured
    assert "DISABLE_BACKGROUND_WORKFLOWS=false\n" in configured
    assert "WORKFLOW_DISPATCHER=inprocess\n" in configured
    second = subprocess.run(
        ["make", "demo-run"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert (tmp_path / ".env").read_text() == configured
