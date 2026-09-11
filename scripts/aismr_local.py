#!/usr/bin/env python3
"""Run AISMR on loopback, with fixtures by default and explicit live configuration."""

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import FrameType
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".local" / "aismr"


# The live launcher consumes exactly these two values from the dedicated
# 1Password FIFO mount. Aliases are only scrubbed from inherited environments.
REQUIRED_CREDENTIAL_KEYS = ("FAL_API_KEY", "OPENAI_API_KEY")
PROVIDER_KEY_NAMES = frozenset(
    {
        "FAL_API_KEY",
        "FAL_KEY",
        "AISMR_FAL_KEY",
        "OPENAI_API_KEY",
        "AISMR_OPENAI_API_KEY",
        "ZERNIO_API_KEY",
        "ZERNIO_KEY",
        "AISMR_ZERNIO_KEY",
    }
)
_RENDERER_BASE_ENV = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ"})
_RUNTIME_CONFIG_KEYS = frozenset(
    {
        "ideation_backend",
        "planner_version",
        "creative_repair_attempts",
        "plan_revisions",
        "preserve_run_artifacts",
        "public_runtime_enabled",
        "private_tailnet_enabled",
        "cookie_secure",
        "session_cookie_suffix",
        "library_database_url",
        "library_media_root",
        "rights_profile_path",
        "cost_profile_version",
        "cost_input_moderation_usd",
        "cost_ideation_usd",
        "cost_plan_moderation_usd",
        "cost_video_request_usd",
        "cost_narration_batch_usd",
        "cost_render_usd",
        "cost_final_moderation_usd",
        "active_runs",
        "visitor_runs_24h",
        "ip_runs_24h",
    }
)
_COST_CONFIG_KEYS = frozenset(
    {
        "cost_input_moderation_usd",
        "cost_ideation_usd",
        "cost_plan_moderation_usd",
        "cost_video_request_usd",
        "cost_narration_batch_usd",
        "cost_render_usd",
        "cost_final_moderation_usd",
    }
)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise argparse.ArgumentTypeError("origin must be a single HTTP(S) origin")
    return value.rstrip("/")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "recorded", "live"), default="fixture")
    parser.add_argument("--recorded-root", type=Path)
    parser.add_argument("--run-budget-usd", default="0")
    parser.add_argument("--daily-budget-usd", default="0")
    parser.add_argument("--api-port", type=_port, default=8311)
    parser.add_argument("--renderer-port", type=_port, default=8312)
    parser.add_argument("--data-root", type=Path, default=DATA)
    parser.add_argument(
        "--origin",
        type=_origin,
        help="Public Studio origin; defaults to the selected loopback API port",
    )
    parser.add_argument(
        "--runtime-config",
        type=Path,
        help="Strictly allowlisted nonsecret live-runtime JSON configuration",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without starting services or calling providers",
    )
    result = parser.parse_args()
    if result.mode == "live":
        try:
            run, daily = Decimal(result.run_budget_usd), Decimal(result.daily_budget_usd)
            valid = run.is_finite() and daily.is_finite() and 0 < run <= daily
        except InvalidOperation:
            valid = False
        if not valid:
            parser.error("live mode requires explicit positive run/daily budgets")
    if result.mode == "recorded" and result.recorded_root is None:
        parser.error("recorded mode requires --recorded-root for the approved media archive")
    if result.api_port == result.renderer_port:
        parser.error("api and renderer ports must differ")
    if result.runtime_config is not None and result.mode != "live":
        parser.error("--runtime-config requires --mode live")
    if result.origin is None:
        result.origin = f"http://127.0.0.1:{result.api_port}"
    result.data_root = result.data_root.expanduser().resolve()
    return result


def local_secrets(data: Path) -> dict[str, str]:
    """Persist only locally generated application secrets, never vendor keys."""
    path = data / "live-session.json"
    names = (
        "AISMR_SESSION_SECRET",
        "REMOTION_API_SECRET",
        "REMOTION_WEBHOOK_SECRET",
        "API_KEY",
    )
    if path.exists():
        values = json.loads(path.read_text(encoding="utf-8"))
    else:
        values = {name: secrets.token_urlsafe(36) for name in names}
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(values, destination)
    # Preserve owner-only access when reusing a stable local secret file.
    os.chmod(path, 0o600)
    if (
        not isinstance(values, dict)
        or set(values) != set(names)
        or any(not isinstance(value, str) or len(value) < 32 for value in values.values())
    ):
        raise RuntimeError("Local live session configuration is invalid")
    return values


def live_values(
    options: argparse.Namespace, run_data: Path, credential_data: Path = DATA
) -> dict[str, str]:
    from dotenv import dotenv_values

    # Read only the dedicated mounted environment. Never source shell text or
    # copy credentials to another file, log, command argument or renderer process.
    mount = credential_data / "credentials.env"
    try:
        mounted_mode = mount.stat().st_mode
    except OSError as exc:
        raise RuntimeError("The AISMR 1Password credential mount is unavailable") from exc
    if not stat.S_ISFIFO(mounted_mode):
        raise RuntimeError("The AISMR credential mount must be the native 1Password FIFO")
    loaded = dotenv_values(mount)
    if any(
        not isinstance(loaded.get(name), str) or not loaded[name]
        for name in REQUIRED_CREDENTIAL_KEYS
    ):
        raise RuntimeError(
            "The AISMR credential mount is missing a required generation or moderation key"
        )
    return {
        **{name: loaded[name] for name in REQUIRED_CREDENTIAL_KEYS},
        **local_secrets(run_data),
        "AISMR_MODE": "live",
        "AISMR_LIVE_ENABLED": "true",
        "AISMR_IDEATION_BACKEND": "codex",
        "AISMR_DAILY_BUDGET_USD": options.daily_budget_usd,
        "AISMR_RUN_RESERVATION_USD": options.run_budget_usd,
        "AISMR_VISITOR_RUNS_24H": "1",
        "AISMR_IP_RUNS_24H": "4",
        "AISMR_ASSET_RETRIES": "0",
        "AISMR_PLAN_REVISIONS": "1",
        "LLAMA_STACK_PROVIDER": "off",
        "USE_FAKE_PROVIDERS": "false",
        "DATABASE_URL": f"sqlite+aiosqlite:///{run_data / 'live.sqlite3'}",
        "AISMR_MEDIA_ROOT": str(run_data / "live-media"),
    }


def runtime_config_values(path: Path, run_data: Path) -> dict[str, str]:
    """Load only explicit nonsecret configuration for an isolated live runtime."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("AISMR runtime config must be readable JSON") from exc
    if not isinstance(raw, dict) or set(raw) - _RUNTIME_CONFIG_KEYS:
        raise RuntimeError("AISMR runtime config contains unsupported or secret fields")

    values: dict[str, str] = {}
    for name, value in raw.items():
        if name == "ideation_backend":
            if value not in {"openai", "codex"}:
                raise RuntimeError("AISMR ideation_backend must be openai or codex")
            values["AISMR_IDEATION_BACKEND"] = value
        elif name == "planner_version":
            if value not in {"single-v1", "creative-v2"}:
                raise RuntimeError("AISMR planner_version must be single-v1 or creative-v2")
            values["AISMR_PLANNER_VERSION"] = value
        elif name in {"creative_repair_attempts", "plan_revisions"}:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2:
                raise RuntimeError(f"AISMR {name} must be an integer from zero through two")
            values[f"AISMR_{name.upper()}"] = str(value)
        elif name in {
            "public_runtime_enabled",
            "private_tailnet_enabled",
            "cookie_secure",
            "preserve_run_artifacts",
        }:
            if not isinstance(value, bool):
                raise RuntimeError(f"AISMR {name} must be boolean")
            values[f"AISMR_{name.upper()}"] = str(value).lower()
        elif name == "session_cookie_suffix":
            if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value) is None:
                raise RuntimeError(
                    "AISMR session_cookie_suffix must be lowercase letters, digits, or underscores"
                )
            values["AISMR_SESSION_COOKIE_SUFFIX"] = value
        elif name in {"active_runs", "visitor_runs_24h", "ip_runs_24h"}:
            limits = {"active_runs": 20, "visitor_runs_24h": 20, "ip_runs_24h": 100}
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= limits[name]
            ):
                raise RuntimeError(f"AISMR {name} is outside its supported range")
            values[f"AISMR_{name.upper()}"] = str(value)
        elif name in _COST_CONFIG_KEYS:
            try:
                amount = Decimal(str(value))
            except InvalidOperation as exc:
                raise RuntimeError(f"AISMR {name} must be a positive decimal") from exc
            if not amount.is_finite() or amount <= 0:
                raise RuntimeError(f"AISMR {name} must be a positive decimal")
            values[f"AISMR_{name.upper()}"] = str(amount)
        elif name in {"library_media_root", "rights_profile_path"}:
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(f"AISMR {name} must be a nonempty path")
            values[f"AISMR_{name.upper()}"] = str(Path(value).expanduser().resolve())
        elif name == "library_database_url":
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError("AISMR library_database_url must be nonempty")
            values["AISMR_LIBRARY_DATABASE_URL"] = value
        elif name == "cost_profile_version":
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError("AISMR cost_profile_version must be nonempty")
            values["AISMR_COST_PROFILE_VERSION"] = value

    live_database = f"sqlite+aiosqlite:///{run_data / 'live.sqlite3'}"
    if values.get("AISMR_LIBRARY_DATABASE_URL") == live_database:
        raise RuntimeError("AISMR library database must differ from the live source database")
    if values.get("AISMR_LIBRARY_MEDIA_ROOT") == str(run_data / "live-media"):
        raise RuntimeError("AISMR library media root must differ from the live source media root")
    return values


def ensure_listener_ports_available(*ports: int) -> None:
    """Fail before spawning children when a loopback listener is already occupied."""
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"AISMR loopback port {port} is already occupied") from exc


def ensure_live_data_root_isolated(data_root: Path) -> None:
    """Keep a live root out of known fixture and recorded source trees."""
    for candidate in (data_root, *data_root.parents):
        for database_name in ("studio.sqlite3", "recorded.sqlite3"):
            marker = candidate / database_name
            if marker.exists():
                raise RuntimeError(
                    f"AISMR live data root {data_root} conflicts with fixture or recorded state at {marker}"
                )


def renderer_callback_allowlist(origin: str) -> str:
    """Match the renderer's exact HTTP and hostname-only HTTPS callback rules."""
    parsed = urlsplit(origin)
    if parsed.scheme == "https":
        if parsed.hostname is None:  # Defensive: arguments() already validates origin.
            raise RuntimeError("AISMR HTTPS origin has no hostname")
        return parsed.hostname
    return origin


def renderer_environment(env: dict[str, str]) -> dict[str, str]:
    """Pass only basic process variables to Remotion, never inherited app values."""
    return {name: env[name] for name in _RENDERER_BASE_ENV if name in env}


@contextmanager
def isolated_configuration_environment(env: dict[str, str]):
    """Temporarily hide inherited provider aliases while reading StudioSettings."""
    original = {name: os.environ.get(name) for name in PROVIDER_KEY_NAMES}
    try:
        for name in PROVIDER_KEY_NAMES:
            os.environ.pop(name, None)
        os.environ.update(env)
        yield
    finally:
        for name in PROVIDER_KEY_NAMES:
            os.environ.pop(name, None)
        os.environ.update({name: value for name, value in original.items() if value is not None})


def renderer_runtime_environment(
    env: dict[str, str], renderer_port: int, *, data_root: Path | None = None
) -> dict[str, str]:
    """Add only local renderer credentials and its selected loopback settings."""
    result = {
        **renderer_environment(env),
        "NODE_ENV": "development",
        "HOST": "127.0.0.1",
        "PORT": str(renderer_port),
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{renderer_port}",
        # These are locally generated renderer-to-API credentials, not vendor keys.
        "WEBHOOK_SECRET": env["REMOTION_WEBHOOK_SECRET"],
        "REMOTION_API_SECRET": env["REMOTION_API_SECRET"],
        "REMOTION_JOB_CONCURRENCY": "1",
        "REMOTION_FRAME_CONCURRENCY": "1",
        "REMOTION_MEDIA_ALLOWED_ORIGINS": env["AISMR_ORIGIN"],
        "REMOTION_CALLBACK_ALLOWLIST": renderer_callback_allowlist(env["AISMR_ORIGIN"]),
        "REMOTION_PRESERVE_NARRATION": env.get("AISMR_PRESERVE_RUN_ARTIFACTS", "false"),
    }
    if data_root is not None:
        result["TMPDIR"] = str(data_root / "render-tmp")
    return result


def prepare_renderer_storage(data_root: Path) -> None:
    """Keep temporary frame work on the same filesystem as renderer outputs."""
    temporary = data_root / "render-tmp"
    if temporary.is_symlink() or (temporary.exists() and not temporary.is_dir()):
        raise RuntimeError("AISMR renderer temporary root must be a real directory")
    if temporary.exists() and temporary.stat().st_uid != os.getuid():
        raise RuntimeError("AISMR renderer temporary root must belong to the current user")
    if shutil.disk_usage(data_root).free < 4 * 1024**3:
        raise RuntimeError("AISMR renderer requires at least 4 GiB of free disk space")
    temporary.mkdir(mode=0o700, exist_ok=True)
    temporary.chmod(0o700)


def check_configuration(env: dict[str, str], mode: str) -> None:
    """Validate configuration only. This function starts no service or provider call."""
    with isolated_configuration_environment(env):
        from myloware.config.studio import StudioSettings

        settings = StudioSettings()
        if settings.live_configuration_errors():
            raise RuntimeError("AISMR live configuration is incomplete")
    print(f"AISMR {mode} configuration is valid. No provider requests or services started.")


def process_commands(
    options: argparse.Namespace, env: dict[str, str], render_env: dict[str, str]
) -> list[tuple[list[str], Path, dict[str, str], str]]:
    """Build isolated child commands without starting a process."""
    python = str(ROOT / ".venv/bin/python")
    renderer_server = str((ROOT / "services/remotion/dist/api/server.js").resolve())
    return [
        (
            ["node", renderer_server],
            options.data_root,
            render_env,
            "renderer",
        ),
        (
            [
                python,
                "-m",
                "uvicorn",
                "myloware.api.server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(options.api_port),
                "--no-access-log",
                "--no-proxy-headers",
            ],
            ROOT,
            env,
            "api",
        ),
    ]


def use_checkout_source(env: dict[str, str]) -> None:
    """Use this checkout for configuration, API and worker imports."""
    source = str(ROOT / "src")
    sys.path.insert(0, source)
    env["PYTHONPATH"] = source


def main() -> None:
    options = arguments()
    if options.mode == "live":
        ensure_live_data_root_isolated(options.data_root)
    options.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(options.data_root, 0o700)
    env = dict(os.environ)
    use_checkout_source(env)
    env.update(
        {
            "AISMR_ENABLED": "true",
            "AISMR_MODE": "fixture",
            "AISMR_RENDER_REAL": "true",
            "AISMR_ORIGIN": options.origin,
            "AISMR_COOKIE_SECURE": "false",
            "AISMR_PUBLIC_RUNTIME_ENABLED": "false",
            "AISMR_MEDIA_ROOT": str(options.data_root / "media"),
            "AISMR_FIXTURE_ROOT": str(options.data_root / "fixtures"),
            "AISMR_FIXTURE_MODERATION": "allow",
            "AISMR_VISITOR_RUNS_24H": "20",
            "AISMR_IP_RUNS_24H": "100",
            "AISMR_ACTIVE_RUNS": "2",
            "AISMR_POLL_SECONDS": "2",
            "DATABASE_URL": f"sqlite+aiosqlite:///{options.data_root / 'studio.sqlite3'}",
            "LLAMA_STACK_PROVIDER": "fake",
            "SORA_PROVIDER": "off",
            "UPLOAD_POST_PROVIDER": "off",
            "REMOTION_PROVIDER": "real",
            "USE_FAKE_PROVIDERS": "true",
            "USE_LANGGRAPH_ENGINE": "true",
            "REMOTION_SERVICE_URL": f"http://127.0.0.1:{options.renderer_port}",
            "REMOTION_API_SECRET": "aismr-loopback-fixture-renderer",
            "REMOTION_WEBHOOK_SECRET": "aismr-loopback-fixture-callback",
            "DISABLE_BACKGROUND_WORKFLOWS": "false",
            "WORKER_CONCURRENCY": "2",
            "JOB_LEASE_SECONDS": "120",
            "JOB_POLL_INTERVAL_SECONDS": "1",
            "OTEL_SDK_DISABLED": "true",
            "SENTRY_DSN": "",
            "ENVIRONMENT": "development",
        }
    )
    if options.mode == "live":
        for name in PROVIDER_KEY_NAMES:
            env.pop(name, None)
        env.update(live_values(options, options.data_root))
        if options.runtime_config is not None:
            env.update(runtime_config_values(options.runtime_config, options.data_root))
    else:
        env["AISMR_LIVE_ENABLED"] = "false"
        for name in PROVIDER_KEY_NAMES:
            env.pop(name, None)
        if options.mode == "recorded":
            env.update(
                {
                    "AISMR_MODE": "recorded",
                    "AISMR_RECORDED_ROOT": str(options.recorded_root.resolve()),
                    "DATABASE_URL": f"sqlite+aiosqlite:///{options.data_root / 'recorded.sqlite3'}",
                    "AISMR_MEDIA_ROOT": str(options.data_root / "recorded-media"),
                    "AISMR_PLAN_REVISIONS": "0",
                    "AISMR_MUSIC_ID": "tender-moment",
                }
            )
    # Explicit JSON values override any incompatible older comma-separated .env defaults.
    env["AISMR_MEDIA_ALLOWED_ORIGINS"] = json.dumps(
        ["https://fal.media", "https://v3.fal.media", "https://v3b.fal.media"]
    )
    if options.mode == "live":
        # Validate the complete live contract before opening either listener.
        check_configuration(env, options.mode)
    if options.check_config:
        os.chdir(ROOT)
        if options.mode != "live":
            check_configuration(env, options.mode)
        return
    ensure_listener_ports_available(options.api_port, options.renderer_port)
    prepare_renderer_storage(options.data_root)
    render_env = renderer_runtime_environment(
        env, options.renderer_port, data_root=options.data_root
    )
    commands = process_commands(options, env, render_env)
    children = []
    files = []
    stopping = False

    def request_stop(signum: int, frame: FrameType | None) -> None:
        # Do not raise inside Popen: the returned child must first be recorded
        # so cleanup cannot lose a process started just as launchd stops us.
        nonlocal stopping
        stopping = True

    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for sig in previous_handlers:
            signal.signal(sig, request_stop)
        for command, cwd, child_env, label in commands:
            if stopping:
                return
            log = (options.data_root / f"{label}.log").open("a")
            files.append(log)
            children.append(
                subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=child_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            )
        for _ in range(80):
            if stopping:
                return
            if any(child.poll() is not None for child in children):
                raise RuntimeError(f"A local service exited; inspect {options.data_root}/*.log")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{options.api_port}/v1/studio/config", timeout=1
                ) as response:
                    if response.status == 200:
                        break
            except (OSError, TimeoutError):
                time.sleep(0.25)
        else:
            raise RuntimeError("The API did not become ready")
        if stopping:
            return
        log = (options.data_root / "worker.log").open("a")
        files.append(log)
        children.append(
            subprocess.Popen(
                [str(ROOT / ".venv/bin/aismr"), "worker", "run"],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        )
        (options.data_root / "local-pids.txt").write_text(
            "\n".join(str(child.pid) for child in children) + "\n"
        )
        print(f"AISMR {options.mode} studio: {options.origin}", flush=True)
        print("Two queue workers, one render job, one frame worker.", flush=True)
        print(
            (
                "Live generation requires the visitor's plan approval and configured budget."
                if options.mode == "live"
                else "Recorded or fixture media; provider calls are disabled."
            ),
            flush=True,
        )
        while all(child.poll() is None for child in children):
            if stopping:
                return
            time.sleep(0.5)
        if stopping:
            return
        raise RuntimeError(f"A local service exited; inspect {options.data_root}/*.log")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            for child in children:
                if child.poll() is None:
                    child.send_signal(signal.SIGTERM)
            for child in children:
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        finally:
            for log in files:
                log.close()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    main()
