from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest


@pytest.fixture
def launcher() -> object:
    path = Path("scripts/aismr_local.py")
    spec = importlib.util.spec_from_file_location("aismr_launcher_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_arguments_require_positive_budgets_without_publisher(
    launcher: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["aismr_local.py", "--mode", "live"])
    with pytest.raises(SystemExit):
        launcher.arguments()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "aismr_local.py",
            "--mode",
            "live",
            "--run-budget-usd",
            "0.01",
            "--daily-budget-usd",
            "0.02",
        ],
    )
    options = launcher.arguments()
    assert options.mode == "live"


def test_arguments_support_an_isolated_public_origin(
    launcher: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "aismr_local.py",
            "--mode",
            "live",
            "--run-budget-usd",
            "0.45",
            "--daily-budget-usd",
            "0.45",
            "--api-port",
            "8452",
            "--renderer-port",
            "8453",
            "--data-root",
            "/private/tmp/aismr-live",
            "--origin",
            "https://live.example.test",
            "--runtime-config",
            "/private/tmp/aismr-live/runtime.json",
        ],
    )

    options = launcher.arguments()

    assert options.api_port == 8452
    assert options.renderer_port == 8453
    assert options.origin == "https://live.example.test"
    assert options.data_root == Path("/private/tmp/aismr-live")


def test_arguments_default_origin_tracks_selected_api_port(
    launcher: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["aismr_local.py", "--api-port", "8452"])

    assert launcher.arguments().origin == "http://127.0.0.1:8452"


def test_live_values_allow_only_native_fifo_and_two_required_vendor_keys(
    launcher: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = tmp_path / "aismr"
    data.mkdir()
    mount = data / "credentials.env"
    os.mkfifo(mount, 0o600)
    monkeypatch.setitem(
        sys.modules,
        "dotenv",
        SimpleNamespace(
            dotenv_values=lambda _path: {
                "FAL_API_KEY": "test-fal",
                "OPENAI_API_KEY": "test-openai",
                "ZERNIO_API_KEY": "test-zernio",
                "EXTRA": "not-forwarded",
            }
        ),
    )
    options = SimpleNamespace(
        daily_budget_usd="0.02",
        run_budget_usd="0.01",
        tiktok_account_id="account-1",
        tiktok_account_name="AISMR demo account",
    )

    values = launcher.live_values(options, data, credential_data=data)

    assert {name for name in values if name.endswith("_API_KEY")} == {
        "FAL_API_KEY",
        "OPENAI_API_KEY",
    }
    assert "API_KEY" in values  # locally generated application secret, not a vendor key
    assert values["DATABASE_URL"].endswith("/live.sqlite3")
    assert values["AISMR_MEDIA_ROOT"].endswith("/live-media")
    assert stat.S_IMODE((data / "live-session.json").stat().st_mode) == 0o600


def test_renderer_environment_strips_vendor_and_inherited_secrets(
    launcher: object,
) -> None:
    env = {
        "PATH": "/usr/bin",
        "FAL_API_KEY": "vendor",
        "OPENAI_API_KEY": "vendor",
        "ZERNIO_API_KEY": "vendor",
        "API_KEY": "app",
        "AISMR_SESSION_SECRET": "local",
        "UNRELATED_TOKEN": "also-secret",
        "NORMAL": "allowed",
    }

    result = launcher.renderer_environment(env)

    assert result == {"PATH": "/usr/bin"}


def test_renderer_adds_only_its_two_local_credentials(launcher: object) -> None:
    result = launcher.renderer_runtime_environment(
        {
            "PATH": "/usr/bin",
            "FAL_API_KEY": "vendor",
            "REMOTION_WEBHOOK_SECRET": "callback",
            "REMOTION_API_SECRET": "renderer-api",
            "AISMR_ORIGIN": "http://127.0.0.1:8311",
        },
        8453,
    )

    assert result["WEBHOOK_SECRET"] == "callback"
    assert result["REMOTION_API_SECRET"] == "renderer-api"
    assert "FAL_API_KEY" not in result
    assert result["PORT"] == "8453"


@pytest.mark.parametrize(
    ("origin", "expected_callback_allowlist"),
    [
        ("http://127.0.0.1:8311", "http://127.0.0.1:8311"),
        ("https://mylos-mac-mini.tail0c4e0a.ts.net:8452", "mylos-mac-mini.tail0c4e0a.ts.net"),
    ],
)
def test_renderer_callback_allowlist_matches_renderer_scheme_rules(
    launcher: object, origin: str, expected_callback_allowlist: str
) -> None:
    result = launcher.renderer_runtime_environment(
        {
            "REMOTION_WEBHOOK_SECRET": "callback",
            "REMOTION_API_SECRET": "renderer-api",
            "AISMR_ORIGIN": origin,
        },
        8453,
    )

    assert result["REMOTION_CALLBACK_ALLOWLIST"] == expected_callback_allowlist


def test_check_environment_hides_and_restores_provider_aliases(
    launcher: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAL_API_KEY", "inherited")
    with launcher.isolated_configuration_environment({"AISMR_MODE": "fixture"}):
        assert "FAL_API_KEY" not in os.environ
    assert os.environ["FAL_API_KEY"] == "inherited"


def test_live_values_rejects_a_regular_credentials_file(launcher: object, tmp_path: Path) -> None:
    data = tmp_path / "aismr"
    data.mkdir()
    (data / "credentials.env").write_text("FAL_API_KEY=test", encoding="utf-8")
    options = SimpleNamespace(
        daily_budget_usd="0.02",
        run_budget_usd="0.01",
        tiktok_account_id="account-1",
        tiktok_account_name="AISMR demo account",
    )

    with pytest.raises(RuntimeError, match="native 1Password FIFO"):
        launcher.live_values(options, data, credential_data=data)


def test_runtime_config_maps_only_allowlisted_nonsecret_live_values(
    launcher: object, tmp_path: Path
) -> None:
    run_data = tmp_path / "live"
    config = tmp_path / "runtime.json"
    config.write_text(
        """{
          "ideation_backend": "openai",
          "public_runtime_enabled": true,
          "cookie_secure": true,
          "session_cookie_suffix": "live_trial",
          "library_database_url": "sqlite+aiosqlite:////private/tmp/aismr-library.sqlite3",
          "library_media_root": "/private/tmp/aismr-library-media",
          "rights_profile_path": "/private/tmp/rights.json",
          "cost_profile_version": "owner-reviewed-live-v1",
          "cost_input_moderation_usd": "0.01",
          "cost_ideation_usd": "0.02",
          "cost_plan_moderation_usd": "0.01",
          "cost_video_request_usd": "0.015",
          "cost_narration_batch_usd": "0.10",
          "cost_render_usd": "0.05",
          "cost_final_moderation_usd": "0.01",
          "active_runs": 1,
          "visitor_runs_24h": 1,
          "ip_runs_24h": 4
        }""",
        encoding="utf-8",
    )

    values = launcher.runtime_config_values(config, run_data)

    assert values["AISMR_IDEATION_BACKEND"] == "openai"
    assert values["AISMR_PUBLIC_RUNTIME_ENABLED"] == "true"
    assert values["AISMR_COOKIE_SECURE"] == "true"
    assert values["AISMR_SESSION_COOKIE_SUFFIX"] == "live_trial"
    assert values["AISMR_COST_VIDEO_REQUEST_USD"] == "0.015"
    assert values["AISMR_ACTIVE_RUNS"] == "1"
    assert "FAL_API_KEY" not in values


def test_runtime_config_rejects_secret_and_source_root_collisions(
    launcher: object, tmp_path: Path
) -> None:
    run_data = tmp_path / "live"
    secret_config = tmp_path / "secret.json"
    secret_config.write_text('{"openai_api_key": "not-allowed"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="unsupported or secret"):
        launcher.runtime_config_values(secret_config, run_data)

    collision = tmp_path / "collision.json"
    collision.write_text(
        json.dumps({"library_media_root": str(run_data / "live-media")}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="library media root"):
        launcher.runtime_config_values(collision, run_data)


def test_private_trial_config_keeps_creative_repair_and_artifact_retention(
    launcher: object, tmp_path: Path
) -> None:
    config = tmp_path / "trial.json"
    config.write_text(
        json.dumps(
            {
                "planner_version": "creative-v2",
                "creative_repair_attempts": 2,
                "plan_revisions": 1,
                "preserve_run_artifacts": True,
                "private_tailnet_enabled": True,
            }
        )
    )
    values = launcher.runtime_config_values(config, tmp_path / "live")
    assert values == {
        "AISMR_PLANNER_VERSION": "creative-v2",
        "AISMR_CREATIVE_REPAIR_ATTEMPTS": "2",
        "AISMR_PLAN_REVISIONS": "1",
        "AISMR_PRESERVE_RUN_ARTIFACTS": "true",
        "AISMR_PRIVATE_TAILNET_ENABLED": "true",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("planner_version", "unreviewed"),
        ("creative_repair_attempts", True),
        ("creative_repair_attempts", 3),
        ("plan_revisions", -1),
        ("preserve_run_artifacts", "false"),
        ("private_tailnet_enabled", "true"),
    ],
)
def test_trial_config_rejects_invalid_effect_controls(
    launcher: object, tmp_path: Path, field: str, value: object
) -> None:
    config = tmp_path / "trial.json"
    config.write_text(json.dumps({field: value}))
    with pytest.raises(RuntimeError):
        launcher.runtime_config_values(config, tmp_path / "live")


def test_renderer_storage_uses_private_runtime_filesystem(
    launcher: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher.shutil, "disk_usage", lambda _: SimpleNamespace(free=5 * 1024**3))
    launcher.prepare_renderer_storage(tmp_path)
    result = launcher.renderer_runtime_environment(
        {
            "TMPDIR": "/unrelated/filesystem",
            "FAL_API_KEY": "must-not-reach-renderer",
            "AISMR_ORIGIN": "http://127.0.0.1:8311",
            "REMOTION_API_SECRET": "renderer",
            "REMOTION_WEBHOOK_SECRET": "callback",
            "AISMR_PRESERVE_RUN_ARTIFACTS": "true",
        },
        8312,
        data_root=tmp_path,
    )
    assert result["TMPDIR"] == str(tmp_path / "render-tmp")
    assert stat.S_IMODE((tmp_path / "render-tmp").stat().st_mode) == 0o700
    assert result["REMOTION_PRESERVE_NARRATION"] == "true"
    assert "FAL_API_KEY" not in result


def test_renderer_storage_refuses_redirected_or_full_filesystem(
    launcher: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (tmp_path / "render-tmp").symlink_to(unrelated, target_is_directory=True)
    with pytest.raises(RuntimeError, match="real directory"):
        launcher.prepare_renderer_storage(tmp_path)
    (tmp_path / "render-tmp").unlink()
    monkeypatch.setattr(launcher.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024))
    with pytest.raises(RuntimeError, match="4 GiB"):
        launcher.prepare_renderer_storage(tmp_path)
    assert not (tmp_path / "render-tmp").exists()


def test_port_guard_fails_before_child_processes(
    launcher: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OccupiedSocket:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def bind(self, _address: object) -> None:
            raise OSError("occupied")

    monkeypatch.setattr(launcher.socket, "socket", lambda *_args: OccupiedSocket())

    with pytest.raises(RuntimeError, match="8452 is already occupied"):
        launcher.ensure_listener_ports_available(8452, 8453)


@pytest.mark.parametrize("database_name", ["studio.sqlite3", "recorded.sqlite3"])
def test_live_data_root_rejects_fixture_or_recorded_database_before_startup(
    launcher: object, tmp_path: Path, database_name: str
) -> None:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    marker = fixture_root / database_name
    marker.touch()

    with pytest.raises(RuntimeError, match=str(marker)):
        launcher.ensure_live_data_root_isolated(fixture_root)
    with pytest.raises(RuntimeError, match=str(marker)):
        launcher.ensure_live_data_root_isolated(fixture_root / "new-live-child")


def test_live_data_root_allows_an_existing_isolated_live_restart(
    launcher: object, tmp_path: Path
) -> None:
    live_root = tmp_path / "live"
    live_root.mkdir()
    (live_root / "live.sqlite3").touch()
    (live_root / "local-pids.txt").touch()
    (live_root / "output").mkdir()

    launcher.ensure_live_data_root_isolated(live_root)


def test_live_data_root_rejects_fixture_state_at_the_default_root(
    launcher: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default_root = tmp_path / "default"
    default_root.mkdir()
    marker = default_root / "studio.sqlite3"
    marker.touch()
    monkeypatch.setattr(launcher, "DATA", default_root)

    with pytest.raises(RuntimeError, match=str(marker)):
        launcher.ensure_live_data_root_isolated(default_root)


def test_process_commands_keep_renderer_outputs_and_api_separate(
    launcher: object, tmp_path: Path
) -> None:
    data_root = tmp_path / "isolated-live"
    options = SimpleNamespace(api_port=8452, data_root=data_root)
    env = {"AISMR_ORIGIN": "https://live.example.test"}
    render_env = {"PORT": "8453"}

    renderer, api = launcher.process_commands(options, env, render_env)

    renderer_command, renderer_cwd, renderer_child_env, renderer_label = renderer
    assert renderer_label == "renderer"
    assert renderer_cwd == data_root
    assert renderer_child_env is render_env
    assert renderer_command == [
        "node",
        str((Path("services/remotion/dist/api/server.js")).resolve()),
    ]
    api_command, api_cwd, api_child_env, api_label = api
    assert api_label == "api"
    assert api_cwd == launcher.ROOT
    assert api_child_env is env
    assert api_command[api_command.index("--port") + 1] == "8452"
    assert data_root / "renderer.log" != launcher.DATA / "renderer.log"
    assert data_root / "worker.log" != launcher.DATA / "worker.log"


def test_launcher_and_children_import_the_current_checkout(tmp_path: Path) -> None:
    launcher_path = Path("scripts/aismr_local.py").resolve()
    code = """
import importlib.util, json, os, subprocess, sys
spec = importlib.util.spec_from_file_location('local_launcher', sys.argv[1])
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
env = dict(os.environ)
launcher.use_checkout_source(env)
parent = importlib.util.find_spec('myloware').origin
child = subprocess.check_output(
    [sys.executable, '-c',
     'import importlib.util; print(importlib.util.find_spec("myloware").origin)'],
    env=env, text=True,
).strip()
print(json.dumps([parent, child]))
"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.check_output(
        [sys.executable, "-c", code, str(launcher_path)],
        cwd=tmp_path,
        env=env,
        text=True,
    )
    expected = str(Path("src/myloware/__init__.py").resolve())
    assert json.loads(result) == [expected, expected]


@pytest.mark.parametrize("phase", ["startup", "running"])
@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_native_signal_stops_and_reaps_owned_services(
    tmp_path: Path, phase: str, stop_signal: signal.Signals
) -> None:
    """Exercise real parent/child signals without starting application services."""
    harness = r'''
import importlib.util, os, signal, subprocess, sys
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("launcher", sys.argv[1])
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
root = Path(sys.argv[2])
phase = sys.argv[3]
launcher.arguments = lambda: SimpleNamespace(
    mode="fixture", data_root=root, origin="http://127.0.0.1:8311",
    api_port=8311, renderer_port=8312, check_config=False,
)
launcher.ensure_listener_ports_available = lambda *args: None
launcher.prepare_renderer_storage = lambda *args: None
launcher.renderer_runtime_environment = lambda *args, **kwargs: {}
launcher.process_commands = lambda *args: [
    (["renderer"], root, {}, "renderer"), (["api"], root, {}, "api")
]
child_code = r"""
import os, signal, sys, time
from pathlib import Path
root = Path(sys.argv[1])
def stop(*args):
    (root / (str(os.getpid()) + ".stopped")).touch()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, signal.SIG_IGN)
(root / (str(os.getpid()) + ".ready")).touch()
while True:
    time.sleep(0.05)
"""
original_popen = subprocess.Popen
def spawn(command, **kwargs):
    return original_popen([sys.executable, "-c", child_code, str(root)], **kwargs)
launcher.subprocess.Popen = spawn
class Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *args): pass
def probe(*args, **kwargs):
    if phase == "startup":
        (root / "waiting-for-api").touch()
        raise OSError("API deliberately not ready")
    return Response()
launcher.urllib.request.urlopen = probe
launcher.main()
'''
    expected_children = 2 if phase == "startup" else 3
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            harness,
            str(Path("scripts/aismr_local.py").resolve()),
            str(tmp_path),
            phase,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while len(list(tmp_path.glob("*.ready"))) != expected_children:
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "owned services did not start"
            time.sleep(0.02)
        pids = [int(path.stem) for path in tmp_path.glob("*.ready")]
        process.send_signal(stop_signal)
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 0, (stdout, stderr)
        assert len(list(tmp_path.glob("*.stopped"))) == expected_children
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        # This isolated group contains only the harness and its dummy children.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=5)
