"""Small, fail-closed filesystem retention for completed studio videos."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class LibraryCandidate:
    run_id: UUID
    status: str
    mode: str
    final_hash: str | None
    final_metadata: dict[str, object] | None
    completed_at: datetime | None


@dataclass(frozen=True)
class RetentionReceipt:
    kept_run_ids: tuple[UUID, ...]
    evicted_run_ids: tuple[UUID, ...]
    removed_paths: tuple[str, ...]
    removed_bytes: int
    skipped: tuple[str, ...]


@dataclass(frozen=True)
class RetentionPlan:
    """A durable-action-ready selection; applying it never recalculates eligibility."""

    kept: tuple[LibraryCandidate, ...]
    evicted: tuple[LibraryCandidate, ...]
    terminal: tuple[LibraryCandidate, ...]
    noop: tuple[LibraryCandidate, ...]


class StudioLibrary:
    """Retain verified finals for playback while exposing only non-fixture gallery items."""

    def __init__(self, *, media_root: Path, fixture_root: Path) -> None:
        self.media_root = self._root(media_root)
        self.fixture_root = self._root(fixture_root)

    def prune(self, candidates: list[LibraryCandidate]) -> RetentionReceipt:
        return self.apply(self.plan(candidates))

    def plan(self, candidates: list[LibraryCandidate]) -> RetentionPlan:
        """Select actions, honoring a committed cleanup intent after an interrupted run."""
        pending = {"evict": [], "remove": []}
        eligible: list[LibraryCandidate] = []
        noop: list[LibraryCandidate] = []
        unplanned: list[LibraryCandidate] = []
        for candidate in candidates:
            intent = (candidate.final_metadata or {}).get("retention_cleanup")
            if isinstance(intent, dict) and intent.get("state") == "pending":
                action = intent.get("action")
                if action == "keep":
                    # A prior attempt may already have removed this namespace.
                    # It cannot occupy a retention slot when its final is gone.
                    if self._prunable(candidate):
                        eligible.append(candidate)
                    else:
                        noop.append(candidate)
                    continue
                if action in pending:
                    pending[action].append(candidate)
                    continue
            unplanned.append(candidate)
        eligible.extend(candidate for candidate in unplanned if self._prunable(candidate))
        eligible.sort(key=lambda candidate: candidate.completed_at, reverse=True)
        kept = eligible[:3]
        evicted = [*pending["evict"], *eligible[3:]]
        terminal = [
            *pending["remove"],
            *(
                candidate
                for candidate in unplanned
                if candidate.status in {"failed", "cancelled", "blocked"}
            ),
        ]
        return RetentionPlan(tuple(kept), tuple(evicted), tuple(terminal), tuple(noop))

    def apply(self, plan: RetentionPlan) -> RetentionReceipt:
        """Apply a previously committed selection using only owned, known paths."""
        removed: list[str] = []
        removed_bytes = [0]
        skipped: list[str] = []
        for candidate in plan.kept:
            self._clean_directory(
                self._run_dir(self.media_root, candidate.run_id),
                keep_final=True,
                removed=removed,
                skipped=skipped,
                bytes_removed=removed_bytes,
            )
            self._remove_directory(
                self._run_dir(self.fixture_root, candidate.run_id), removed, skipped, removed_bytes
            )
        for candidate in plan.evicted:
            self._remove_directory(
                self._run_dir(self.media_root, candidate.run_id), removed, skipped, removed_bytes
            )
            self._remove_directory(
                self._run_dir(self.fixture_root, candidate.run_id), removed, skipped, removed_bytes
            )
        for candidate in plan.terminal:
            self._remove_directory(
                self._run_dir(self.media_root, candidate.run_id), removed, skipped, removed_bytes
            )
            self._remove_directory(
                self._run_dir(self.fixture_root, candidate.run_id), removed, skipped, removed_bytes
            )
        return RetentionReceipt(
            tuple(item.run_id for item in plan.kept),
            tuple(item.run_id for item in plan.evicted),
            tuple(removed),
            removed_bytes[0],
            tuple(skipped),
        )

    def retained(self, candidates: list[LibraryCandidate]) -> tuple[LibraryCandidate, ...]:
        """Read-only gallery selection. This method never changes the filesystem."""
        eligible = [candidate for candidate in candidates if self._eligible(candidate)]
        eligible.sort(key=lambda candidate: candidate.completed_at, reverse=True)
        return tuple(eligible[:3])

    def remove_projected_source(self, run_id: UUID) -> RetentionReceipt:
        """Remove only known owned source bytes after verified public projection."""
        removed: list[str] = []
        skipped: list[str] = []
        removed_bytes = [0]
        self._remove_directory(
            self._run_dir(self.media_root, run_id), removed, skipped, removed_bytes
        )
        self._remove_directory(
            self._run_dir(self.fixture_root, run_id), removed, skipped, removed_bytes
        )
        return RetentionReceipt((), (), tuple(removed), removed_bytes[0], tuple(skipped))

    def _prunable(self, candidate: LibraryCandidate) -> bool:
        if (
            candidate.status != "video_complete"
            or not candidate.final_hash
            or candidate.completed_at is None
        ):
            return False
        metadata = candidate.final_metadata or {}
        if metadata.get("media_verified") is not True:
            return False
        try:
            final = self._run_dir(self.media_root, candidate.run_id) / "final.mp4"
        except ValueError:
            return False
        return (
            self._regular(final)
            and hashlib.sha256(final.read_bytes()).hexdigest() == candidate.final_hash
        )

    def _eligible(self, candidate: LibraryCandidate) -> bool:
        if candidate.mode == "fixture":
            return False
        return self._prunable(candidate)

    @staticmethod
    def _root(value: Path) -> Path:
        lexical = value.absolute()
        for ancestor in (lexical, *lexical.parents):
            # macOS exposes these as system compatibility aliases. User-created
            # symlink parents remain forbidden before canonicalization.
            # no temporary file is created here; these are OS symlink allowlist entries.
            if ancestor.is_symlink() and ancestor not in {Path("/tmp"), Path("/var")}:  # nosec B108
                raise ValueError("studio retention root has a symlink ancestor")
        root = lexical.resolve(strict=False)
        cwd = Path.cwd().resolve()
        if (
            not root.is_absolute()
            or root == root.parent
            or value.is_symlink()
            or root == cwd
            or root in cwd.parents
        ):
            raise ValueError("studio retention root is unsafe")
        return root

    @staticmethod
    def _regular(path: Path) -> bool:
        return path.exists() and path.is_file() and not path.is_symlink()

    @staticmethod
    def _run_dir(root: Path, run_id: UUID) -> Path:
        directory = root / str(run_id)
        if directory.parent != root or directory.is_symlink():
            raise ValueError("studio retention run directory is unsafe")
        return directory

    def _clean_directory(
        self,
        directory: Path,
        *,
        keep_final: bool,
        removed: list[str],
        skipped: list[str],
        bytes_removed: list[int],
    ) -> None:
        if not directory.exists():
            return
        if not directory.is_dir() or directory.is_symlink():
            skipped.append(f"unsafe:{directory}")
            return
        for child in directory.iterdir():
            if keep_final and child.name == "final.mp4" and self._regular(child):
                continue
            self._remove(
                child,
                removed,
                skipped,
                bytes_removed,
                fixture=directory.parent == self.fixture_root,
            )

    def _remove_directory(
        self, directory: Path, removed: list[str], skipped: list[str], bytes_removed: list[int]
    ) -> None:
        if not directory.exists():
            return
        self._clean_directory(
            directory,
            keep_final=False,
            removed=removed,
            skipped=skipped,
            bytes_removed=bytes_removed,
        )
        if directory.exists() and not directory.is_symlink():
            if any(directory.iterdir()):
                skipped.append(f"nonempty:{directory}")
            else:
                directory.rmdir()
                removed.append(str(directory))

    def _remove(
        self,
        path: Path,
        removed: list[str],
        skipped: list[str],
        bytes_removed: list[int],
        *,
        fixture: bool = False,
    ) -> None:
        if path.is_symlink():
            skipped.append(f"symlink:{path}")
        elif path.is_file() and self._known_file(path.name, fixture=fixture):
            bytes_removed[0] += path.stat().st_size
            path.unlink()
            removed.append(str(path))
        elif path.is_dir() and self._known_directory(path.name, fixture=fixture):
            for child in path.iterdir():
                self._remove(child, removed, skipped, bytes_removed, fixture=fixture)
            if not any(path.iterdir()):
                path.rmdir()
                removed.append(str(path))
        else:
            skipped.append(f"unknown:{path}")

    @staticmethod
    def _known_file(name: str, *, fixture: bool) -> bool:
        if name == "final.mp4":
            return True
        if fixture:
            return bool(
                re.fullmatch(
                    r"(?:month-\d{2}\.(?:mp4|wav)|voice-batch(?:-timestamps)?\.(?:wav|json)|scene-(?:0[1-9]|1[0-2])\.mp4|scene-(?:0[1-9]|1[0-2])-title\.wav|title-batch(?:-timestamps)?\.(?:wav|json))",
                    name,
                )
            )
        return bool(
            re.fullmatch(r"[0-9a-f]{64}(?:\.media|\.frame-\d+-[0-9a-f]+\.jpg)", name)
            or re.fullmatch(r"\d{2}-voice\.wav", name)
        )

    @staticmethod
    def _known_directory(name: str, *, fixture: bool) -> bool:
        return not fixture and bool(re.fullmatch(r"[0-9a-f]{64}-splits", name))
