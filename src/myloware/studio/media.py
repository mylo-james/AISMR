"""Run-scoped, bounded retrieval and technical verification for media assets."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import posixpath
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit

import httpx

_FFMPEG_TIMEOUT_SECONDS = 30.0


class MediaVerificationError(ValueError):
    """Raised before untrusted media can be stored or used."""


@dataclass(frozen=True)
class MediaFetchPolicy:
    allowed_origins: frozenset[str]
    asset_byte_cap: int
    run_byte_cap: int
    allowed_path_prefixes: frozenset[str] = frozenset()
    fixture_mode: bool = False
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not self.allowed_origins or self.asset_byte_cap <= 0 or self.run_byte_cap <= 0:
            raise ValueError("media policy requires origins and positive byte caps")
        for origin in self.allowed_origins:
            _validate_origin(origin, fixture_mode=self.fixture_mode)
        for prefix in self.allowed_path_prefixes:
            parsed = urlsplit(prefix)
            origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
            _validate_origin(origin, fixture_mode=self.fixture_mode)
            if not parsed.path.startswith("/") or not parsed.path.endswith("/"):
                raise MediaVerificationError("media path prefix must have a trailing slash")


@dataclass(frozen=True)
class VerifiedMedia:
    path: Path
    sha256: str
    byte_size: int
    duration_seconds: float
    width: int
    height: int
    frame_paths: tuple[Path, ...]
    metadata: dict[str, Any]


def _validate_origin(origin: str, *, fixture_mode: bool) -> None:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise MediaVerificationError("allowed origin must be an absolute HTTP(S) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MediaVerificationError(
            "allowed origin cannot contain credentials, query, or fragment"
        )
    if parsed.scheme != "https" and not (fixture_mode and _is_loopback_host(parsed.hostname)):
        raise MediaVerificationError(
            "only HTTPS origins are allowed outside explicit loopback fixtures"
        )
    if _is_private_host(parsed.hostname) and not (
        fixture_mode and _is_loopback_host(parsed.hostname)
    ):
        raise MediaVerificationError("private network origins are never allowed")


def _is_loopback_host(host: str | None) -> bool:
    return host in {"localhost", "::1"} or _is_ip_in(host, lambda address: address.is_loopback)


def _is_private_host(host: str | None) -> bool:
    if not host:
        return True
    if host == "localhost":
        return True
    return _is_ip_in(
        host,
        lambda address: address.is_private
        or address.is_link_local
        or address.is_loopback
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified,
    )


def _is_ip_in(host: str | None, predicate: Any) -> bool:
    try:
        return bool(predicate(ipaddress.ip_address(host or "")))
    except ValueError:
        return False


def validate_media_url(url: str, policy: MediaFetchPolicy) -> str:
    """Accept only an exact configured origin, never a redirectable destination."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise MediaVerificationError("media URL must be absolute HTTP(S)")
    if parsed.username or parsed.password or parsed.fragment:
        raise MediaVerificationError("media URL credentials and fragments are forbidden")
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    if origin not in policy.allowed_origins and not any(
        _matches_path_prefix(parsed, prefix) for prefix in policy.allowed_path_prefixes
    ):
        raise MediaVerificationError("media URL origin is not configured for this provider")
    _validate_origin(origin, fixture_mode=policy.fixture_mode)
    return url


def _matches_path_prefix(parsed_url: SplitResult, prefix: str) -> bool:
    """Match a configured storage path after decoding and dot-segment cleanup."""
    configured = urlsplit(prefix)
    if parsed_url.scheme != configured.scheme or parsed_url.netloc != configured.netloc:
        return False
    allowed = _canonical_path(configured.path).rstrip("/") + "/"
    return _canonical_path(parsed_url.path).startswith(allowed)


def _canonical_path(path: str) -> str:
    """Decode bounded URL escaping before normalizing potentially traversable paths."""
    decoded = path
    for _ in range(4):
        candidate = unquote(decoded)
        if candidate == decoded:
            break
        decoded = candidate
    return posixpath.normpath("/" + decoded.lstrip("/"))


async def fetch_verified_media(
    *,
    url: str,
    run_id: str,
    target_root: Path,
    policy: MediaFetchPolicy,
    existing_run_bytes: int = 0,
    kind: str = "video",
    request_headers: dict[str, str] | None = None,
) -> VerifiedMedia:
    """Fetch one allowed asset into ``target_root/run_id`` and verify it fully."""
    validate_media_url(url, policy)
    if existing_run_bytes < 0 or existing_run_bytes >= policy.run_byte_cap:
        raise MediaVerificationError("run media byte cap is exhausted")
    parsed_run_id = str(run_id).strip()
    if not parsed_run_id or any(char not in "0123456789abcdef-" for char in parsed_run_id.lower()):
        raise MediaVerificationError("run_id must be a canonical UUID-like identifier")
    root = target_root.resolve()
    run_dir = (root / parsed_run_id).resolve()
    if root not in run_dir.parents:
        raise MediaVerificationError("run media target escapes the configured root")
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _validate_kind(kind)
    descriptor, temporary_name = tempfile.mkstemp(prefix="incoming-", suffix=".media", dir=run_dir)
    temporary = Path(temporary_name)
    os.close(descriptor)
    downloaded = 0
    digest = sha256()
    try:
        async with (
            httpx.AsyncClient(follow_redirects=False, timeout=policy.timeout_seconds) as client,
            client.stream("GET", url, headers=request_headers) as response,
        ):
            if response.is_redirect:
                raise MediaVerificationError("media redirects are forbidden")
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                downloaded += len(chunk)
                if (
                    downloaded > policy.asset_byte_cap
                    or existing_run_bytes + downloaded > policy.run_byte_cap
                ):
                    raise MediaVerificationError("media byte cap exceeded")
                digest.update(chunk)
                await asyncio.to_thread(_append_chunk, temporary, chunk)
        asset_path = run_dir / f"{digest.hexdigest()}.media"
        temporary.replace(asset_path)
        return await verify_media_file(asset_path, kind=kind)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _validate_kind(kind: str) -> None:
    if kind not in {"video", "audio"}:
        raise MediaVerificationError("media kind must be video or audio")


async def verify_media_file(path: Path, *, kind: str = "video") -> VerifiedMedia:
    """Require a full decode and type-specific stream validation before use."""
    _validate_kind(kind)
    if not path.is_file():
        raise MediaVerificationError("verified media path is missing")
    metadata = await _ffprobe(path)
    streams = metadata.get("streams")
    if not isinstance(streams, list):
        raise MediaVerificationError("ffprobe response has no stream list")
    stream = next((item for item in streams if item.get("codec_type") == kind), None)
    if not isinstance(stream, dict):
        raise MediaVerificationError(f"media has no {kind} stream")
    try:
        duration = float(stream.get("duration") or metadata.get("format", {}).get("duration") or 0)
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    except (TypeError, ValueError) as exc:
        raise MediaVerificationError("media has invalid stream metadata") from exc
    if (
        not math.isfinite(duration)
        or duration <= 0
        or (kind == "video" and (width <= 0 or height <= 0))
    ):
        raise MediaVerificationError("media has invalid duration or dimensions")
    await _run_ffmpeg(("ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"))
    if kind == "audio":
        return VerifiedMedia(
            path=path,
            sha256=sha256(path.read_bytes()).hexdigest(),
            byte_size=path.stat().st_size,
            duration_seconds=duration,
            width=0,
            height=0,
            frame_paths=(),
            metadata=metadata,
        )
    positions = (0.0, duration / 2.0, max(0.0, duration - 0.05))
    frames: list[Path] = []
    for index, position in enumerate(positions):
        frame = path.with_name(f"{path.stem}.frame-{index}-{os.urandom(8).hex()}.jpg")
        await _run_ffmpeg(
            (
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{position:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-y",
                str(frame),
            )
        )
        if not frame.is_file() or frame.stat().st_size == 0:
            raise MediaVerificationError("ffmpeg did not produce a required frame")
        frames.append(frame)
    return VerifiedMedia(
        path=path,
        sha256=sha256(path.read_bytes()).hexdigest(),
        byte_size=path.stat().st_size,
        duration_seconds=duration,
        width=width,
        height=height,
        frame_paths=tuple(frames),
        metadata=metadata,
    )


async def _ffprobe(path: Path) -> dict[str, Any]:
    stdout = await _run_ffmpeg(
        (
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        )
    )
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise MediaVerificationError("ffprobe returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise MediaVerificationError("ffprobe returned an invalid document")
    return parsed


def _append_chunk(path: Path, chunk: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(chunk)


async def _run_ffmpeg(command: tuple[str, ...]) -> str:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_FFMPEG_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise MediaVerificationError("media verifier timed out") from exc
    if process.returncode != 0:
        raise MediaVerificationError(
            f"media verifier failed: {stderr.decode(errors='replace')[:200]}"
        )
    return stdout.decode("utf-8", errors="replace")
