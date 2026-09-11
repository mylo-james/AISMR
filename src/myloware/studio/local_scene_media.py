"""Hash-bound local replay inputs for private scene-v2 review runs.

The archive preserves the distinction between a current approved scene plan and
the older saved title/video material it replays by ordinal.  It never presents
the saved material as a provider generation of the current plan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any


class LocalSceneMediaError(ValueError):
    """A local replay archive is malformed, changed, or unavailable."""


@dataclass(frozen=True)
class LocalSceneMedia:
    path: Path
    sha256: str
    byte_size: int
    duration_seconds: float
    ordinal: int
    source_title: str


class LocalSceneMediaArchive:
    """Read exactly one immutable local scene replay bundle."""

    manifest_name = "local-scene-media-manifest.json"
    contract_version = "local-scene-media-v1"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._manifest_path = self.root / self.manifest_name
        self._manifest_bytes = self._manifest_path.read_bytes()
        try:
            manifest = json.loads(self._manifest_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalSceneMediaError("local scene manifest is invalid") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("contract_version") != self.contract_version
            or not isinstance(manifest.get("archive_id"), str)
            or not manifest["archive_id"].strip()
            or manifest.get("private_only") is not True
        ):
            raise LocalSceneMediaError("local scene manifest contract is invalid")
        self._manifest = manifest

    @property
    def manifest_sha256(self) -> str:
        return sha256(self._manifest_bytes).hexdigest()

    def receipt(self) -> dict[str, str]:
        source = self._manifest.get("source_archive")
        timestamps = self._manifest.get("title_timestamp_receipt")
        if not isinstance(source, dict) or not isinstance(timestamps, dict):
            raise LocalSceneMediaError("local scene manifest receipts are invalid")
        batch = self.batch()
        required = {
            "archive_id": self._manifest.get("archive_id"),
            "manifest_sha256": self.manifest_sha256,
            "contract_version": self.contract_version,
            "source_title_lines_sha256": self._manifest.get("source_title_lines_sha256"),
            "source_title_text_sha256": self._manifest.get("source_title_text_sha256"),
            "source_batch_sha256": source.get("original_batch_sha256"),
            "local_batch_sha256": batch.sha256,
            "source_timestamps_sha256": source.get("original_timestamps_sha256"),
            "title_cuts_sha256": source.get("title_cuts_sha256"),
            "local_timestamps_sha256": timestamps.get("sha256"),
        }
        if not all(isinstance(value, str) and value for value in required.values()):
            raise LocalSceneMediaError("local scene receipt is invalid")
        return required

    def source_title_lines(self) -> tuple[str, ...]:
        lines = self._manifest.get("source_title_lines")
        if (
            not isinstance(lines, list)
            or len(lines) != 12
            or any(not isinstance(line, str) or not line.strip() for line in lines)
            or sha256("\n".join(lines).encode()).hexdigest()
            != self._manifest.get("source_title_lines_sha256")
        ):
            raise LocalSceneMediaError("local scene source title receipt is invalid")
        return tuple(lines)

    def timestamps(self) -> tuple[dict[str, Any], ...]:
        entry = self._manifest.get("title_timestamp_receipt")
        if not isinstance(entry, dict):
            raise LocalSceneMediaError("local scene timestamp receipt is missing")
        path = self._verified_path(entry)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalSceneMediaError("local scene timestamp receipt is invalid") from exc
        timestamps = value.get("timestamps") if isinstance(value, dict) else None
        if not isinstance(timestamps, list) or not all(
            isinstance(item, dict) for item in timestamps
        ):
            raise LocalSceneMediaError("local scene timestamps are invalid")
        expected = self._manifest.get("source_title_text_sha256")
        if value.get("source_title_text_sha256") != expected:
            raise LocalSceneMediaError("local scene timestamp text receipt mismatches")
        return tuple(timestamps)

    def video(self, ordinal: int) -> LocalSceneMedia:
        return self._media("videos", ordinal)

    def title(self, ordinal: int) -> LocalSceneMedia:
        return self._media("title_clips", ordinal)

    def batch(self) -> LocalSceneMedia:
        entry = self._manifest.get("title_batch")
        if not isinstance(entry, dict) or entry.get("ordinal") != 0:
            raise LocalSceneMediaError("local scene title batch is missing")
        return self._as_media(entry)

    def _media(self, collection: str, ordinal: int) -> LocalSceneMedia:
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 1 <= ordinal <= 12:
            raise LocalSceneMediaError("local scene ordinal must be 1 through 12")
        entries = self._manifest.get(collection)
        if not isinstance(entries, list):
            raise LocalSceneMediaError("local scene media list is invalid")
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("ordinal") == ordinal
        ]
        if len(matches) != 1:
            raise LocalSceneMediaError("local scene media entry is missing or ambiguous")
        return self._as_media(matches[0])

    def _as_media(self, entry: dict[str, Any]) -> LocalSceneMedia:
        path = self._verified_path(entry)
        duration = entry.get("duration_seconds")
        source_title = entry.get("source_title", "")
        ordinal = entry.get("ordinal")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or duration <= 0
            or not isinstance(source_title, str)
            or not isinstance(ordinal, int)
        ):
            raise LocalSceneMediaError("local scene media metadata is invalid")
        return LocalSceneMedia(
            path=path,
            sha256=str(entry["sha256"]),
            byte_size=int(entry["byte_size"]),
            duration_seconds=float(duration),
            ordinal=ordinal,
            source_title=source_title,
        )

    def _verified_path(self, entry: dict[str, Any]) -> Path:
        relative, expected_hash, expected_bytes = (
            entry.get("relative_path"),
            entry.get("sha256"),
            entry.get("byte_size"),
        )
        if (
            not isinstance(relative, str)
            or not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or not isinstance(expected_bytes, int)
            or expected_bytes < 1
        ):
            raise LocalSceneMediaError("local scene media receipt is invalid")
        path = (self.root / relative).resolve()
        if self.root not in path.parents or not path.is_file():
            raise LocalSceneMediaError("local scene media path is unavailable")
        if (
            path.stat().st_size != expected_bytes
            or sha256(path.read_bytes()).hexdigest() != expected_hash
        ):
            raise LocalSceneMediaError("local scene media hash mismatch")
        return path
