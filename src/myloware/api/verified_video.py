"""Serve an already-open, hash-verified video through Starlette's range support."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from uuid import UUID

from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send


class VerifiedVideoResponse(FileResponse):
    """Keep the verified inode open through streaming, including an eviction."""

    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        super().__init__(
            f"/dev/fd/{descriptor}",
            stat_result=os.fstat(descriptor),
            media_type="video/mp4",
            headers={"Cache-Control": "no-store"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # The descriptor belongs to this process. Starlette must stream it here,
        # rather than passing a descriptor path to another server process.
        extensions = dict(scope.get("extensions", {}))
        extensions.pop("http.response.pathsend", None)
        try:
            await super().__call__({**scope, "extensions": extensions}, receive, send)
        finally:
            os.close(self.descriptor)


def open_verified_video(root: Path, run_id: UUID, expected_hash: str) -> VerifiedVideoResponse:
    """Open UUID/final.mp4 without following symlinks and verify these exact bytes.

    AISMR's supported local macOS and hosted Linux runtimes expose /dev/fd.
    Reusing FileResponse preserves maintained byte-range and disconnect handling.
    """
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        run_fd = os.open(str(run_id), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            descriptor = os.open(
                "final.mp4", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=run_fd
            )
        finally:
            os.close(run_fd)
    finally:
        os.close(root_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("video is not a regular file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise ValueError("video hash changed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return VerifiedVideoResponse(descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def open_verified_file(path: Path, expected_hash: str) -> VerifiedVideoResponse:
    """Open one already-scoped owned file without following a replacement symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("video is not a regular file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise ValueError("video hash changed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return VerifiedVideoResponse(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
