"""Filesystem primitives for the application-owned public portfolio library."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class PortfolioLibraryError(ValueError):
    """Raised before unsafe media is made public or removed."""


class PortfolioLibraryFiles:
    """Only copies and removes entries inside one validated serving root."""

    def __init__(self, root: Path, *, forbidden_roots: tuple[Path, ...] = ()) -> None:
        self.root = self._validated_root(root, forbidden_roots)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validated_root(value: Path, forbidden_roots: tuple[Path, ...]) -> Path:
        lexical = value.absolute()
        if value.is_symlink() or any(parent.is_symlink() for parent in (lexical, *lexical.parents)):
            raise PortfolioLibraryError("portfolio library root has a symlink ancestor")
        root = lexical.resolve(strict=False)
        cwd = Path.cwd().resolve()
        if root == root.parent or root == cwd or root in cwd.parents:
            raise PortfolioLibraryError("portfolio library root is unsafe")
        for forbidden in forbidden_roots:
            candidate = forbidden.absolute()
            if forbidden.is_symlink() or candidate.is_symlink():
                raise PortfolioLibraryError("portfolio library overlap root is a symlink")
            candidate = candidate.resolve(strict=False)
            if root == candidate or root in candidate.parents or candidate in root.parents:
                raise PortfolioLibraryError("portfolio library root overlaps a protected root")
        return root

    def copy_verified(self, source: Path, serving_key: str, expected_sha256: str) -> Path:
        """Hash while copying to staging, then atomically promote the serving copy."""
        if not self._regular(source):
            raise PortfolioLibraryError("portfolio source final is unavailable")
        target = self.path_for(serving_key)
        if self._regular(target):
            if self.sha256(target) == expected_sha256:
                return target
            raise PortfolioLibraryError("portfolio serving key hash mismatch")
        if target.exists():
            raise PortfolioLibraryError("portfolio serving key is unsafe")
        stage = target.with_name(f".{target.name}.part")
        if stage.exists() and not self._regular(stage):
            raise PortfolioLibraryError("portfolio staging path is unsafe")
        digest = hashlib.sha256()
        try:
            with source.open("rb") as src, stage.open("wb") as dst:
                while chunk := src.read(1024 * 1024):
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if digest.hexdigest() != expected_sha256:
                raise PortfolioLibraryError("portfolio source final hash mismatch")
            os.replace(stage, target)
            return target
        except Exception:
            if stage.exists() and self._regular(stage):
                stage.unlink()
            raise

    def verified(self, serving_key: str, expected_sha256: str) -> Path | None:
        path = self.path_for(serving_key)
        return path if self._regular(path) and self.sha256(path) == expected_sha256 else None

    def remove_owned(self, serving_key: str) -> bool:
        path = self.path_for(serving_key)
        if not path.exists():
            return False
        if not self._regular(path):
            raise PortfolioLibraryError("portfolio serving path is unsafe")
        path.unlink()
        return True

    def path_for(self, serving_key: str) -> Path:
        if (
            not serving_key
            or "/" in serving_key
            or "\\" in serving_key
            or serving_key.startswith(".")
        ):
            raise PortfolioLibraryError("portfolio serving key is invalid")
        path = self.root / f"{serving_key}.mp4"
        if path.parent != self.root or path.is_symlink():
            raise PortfolioLibraryError("portfolio serving path is unsafe")
        return path

    @staticmethod
    def _regular(path: Path) -> bool:
        return path.exists() and path.is_file() and not path.is_symlink()

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
