"""Immutable, hash-bound access to an approved recorded media archive."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from myloware.studio.media import MediaVerificationError
from myloware.workflows.monthly import MonthlyPlan
from myloware.workflows.scenes import parse_plan


class RecordedMediaError(MediaVerificationError):
    """The archive cannot safely stand in for a provider result."""


@dataclass(frozen=True)
class RecordedMedia:
    path: Path
    role: str
    sha256: str
    byte_size: int
    ordinal: int | None = None
    narration: str | None = None


class RecordedMediaArchive:
    """Read one checked, immutable evidence archive without copying its assets."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._entries = self._load_manifest()

    def _load_manifest(self) -> tuple[dict[str, Any], ...]:
        manifest = self.root / "fixture-manifest.json"
        if not manifest.is_file():
            raise RecordedMediaError("recorded fixture manifest is missing")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecordedMediaError("recorded fixture manifest is invalid") from exc
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("entries"), list)
            or not data["entries"]
        ):
            raise RecordedMediaError("recorded fixture manifest has no entries")
        return tuple(entry for entry in data["entries"] if isinstance(entry, dict))

    def _one(self, *, role: str, ordinal: int | None = None) -> RecordedMedia:
        matches = [
            entry
            for entry in self._entries
            if entry.get("role") == role and entry.get("ordinal") == ordinal
        ]
        if len(matches) != 1:
            raise RecordedMediaError("recorded fixture entry is missing or ambiguous")
        entry = matches[0]
        relative = entry.get("relative_path")
        expected_hash, expected_bytes = entry.get("sha256"), entry.get("byte_size")
        if (
            not isinstance(relative, str)
            or not isinstance(expected_hash, str)
            or not isinstance(expected_bytes, int)
        ):
            raise RecordedMediaError("recorded fixture entry is malformed")
        path = (self.root / relative).resolve()
        if self.root not in path.parents or not path.is_file():
            raise RecordedMediaError("recorded fixture path is unavailable")
        stat = path.stat()
        if stat.st_size != expected_bytes or sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise RecordedMediaError("recorded fixture hash or byte size mismatch")
        return RecordedMedia(
            path, role, expected_hash, expected_bytes, ordinal, entry.get("narration")
        )

    def video(self, ordinal: int) -> RecordedMedia:
        return self._one(role="recorded_video_provider_output", ordinal=_ordinal(ordinal))

    def split_voice(self, ordinal: int) -> RecordedMedia:
        return self._one(
            role="recorded_voice_provider_output_pause_split", ordinal=_ordinal(ordinal)
        )

    def batch_voice(self) -> RecordedMedia:
        return self._one(role="recorded_voice_provider_batch")

    def batch_timestamps(self) -> tuple[dict[str, Any], ...]:
        entry = self._one(role="recorded_voice_provider_timestamps")
        try:
            payload = json.loads(entry.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecordedMediaError("recorded narration timestamp receipt is invalid") from exc
        timestamps = payload.get("timestamps") if isinstance(payload, dict) else payload
        if not isinstance(timestamps, list) or not all(
            isinstance(value, dict) for value in timestamps
        ):
            raise RecordedMediaError("recorded narration timestamp receipt is malformed")
        return tuple(timestamps)

    def batch_input_text(self) -> str:
        request = self.root / "receipts" / "voice-request.json"
        try:
            text = json.loads(request.read_text(encoding="utf-8")).get("text")
        except (OSError, json.JSONDecodeError) as exc:
            raise RecordedMediaError("recorded narration request is invalid") from exc
        if not isinstance(text, str) or not text:
            raise RecordedMediaError("recorded narration input text is unavailable")
        return text

    def music(self) -> RecordedMedia:
        return self._one(role="recorded_cc0_music")

    def plan_for(self, *, run_id: UUID, revision: int, item_text: str) -> MonthlyPlan:
        """Bind the archived teacup plan to this visitor run without mutating evidence."""
        entry = self._one(role="accepted_monthly_plan")
        try:
            archived = parse_plan(entry.path.read_text(encoding="utf-8"))
            if not isinstance(archived, MonthlyPlan):
                raise RecordedMediaError("recorded archive requires a legacy v1 plan")
        except Exception as exc:
            raise RecordedMediaError("recorded monthly plan is invalid") from exc
        if (
            archived.item_id != "teacup"
            or archived.item_text.casefold() != "teacup"
            or item_text.casefold() != "teacup"
        ):
            raise RecordedMediaError("recorded media only supports the approved teacup plan")
        if revision < 1:
            raise RecordedMediaError("recorded plan revision must be positive")
        return archived.model_copy(
            update={"run_id": run_id, "revision": revision, "item_text": item_text}
        )

    def all_media(self) -> tuple[RecordedMedia, ...]:
        return (
            tuple(self.video(index) for index in range(1, 13))
            + tuple(self.split_voice(index) for index in range(1, 13))
            + (self.batch_voice(), self.music())
        )


def _ordinal(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
        raise RecordedMediaError("recorded asset ordinal must be 1 through 12")
    return value
