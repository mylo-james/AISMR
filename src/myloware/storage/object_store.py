"""Object storage helpers (optional).

This module provides a small abstraction for storing and retrieving large
artifacts (e.g., transcoded videos) outside the relational database.

It is intentionally dependency-light:
- Default installs do NOT require boto3.
- S3 support is enabled only when configured and when boto3 is installed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from myloware.config import settings

__all__ = [
    "S3ObjectRef",
    "build_s3_uri",
    "get_s3_store",
    "parse_s3_uri",
    "resolve_s3_uri_async",
]


@dataclass(frozen=True)
class S3ObjectRef:
    bucket: str
    key: str


def build_s3_uri(*, bucket: str, key: str) -> str:
    key_norm = key.lstrip("/")
    return f"s3://{bucket}/{key_norm}"


def parse_s3_uri(uri: str) -> S3ObjectRef:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError("Not an s3:// URI")
    bucket = parsed.netloc
    key = (parsed.path or "").lstrip("/")
    if not bucket or not key:
        raise ValueError("Invalid s3:// URI (missing bucket or key)")
    return S3ObjectRef(bucket=bucket, key=key)


def _require_boto3() -> Any:
    try:
        import boto3  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "S3 backend requires boto3. Install with `pip install 'myloware[s3]'` "
            "or add boto3 to your environment."
        ) from exc
    return boto3


class S3Store:
    """Minimal S3 helper for uploads + presigned GET URLs."""

    def __init__(self) -> None:
        from botocore.config import Config

        boto3 = _require_boto3()
        endpoint_url = settings.transcode_s3_endpoint_url or None
        region_name = settings.transcode_s3_region or None
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=15,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    async def upload_file_async(
        self,
        *,
        bucket: str,
        key: str,
        path: Path,
        content_type: str = "application/octet-stream",
    ) -> str:
        if not path.exists():
            raise FileNotFoundError(f"Upload source missing: {path}")

        def _upload() -> None:
            self._client.upload_file(
                str(path),
                bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )

        await asyncio.to_thread(_upload)
        return build_s3_uri(bucket=bucket, key=key)

    async def presign_get_async(self, *, uri: str, expires_seconds: int) -> str:
        ref = parse_s3_uri(uri)

        def _presign() -> str:
            return str(
                self._client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": ref.bucket, "Key": ref.key},
                    ExpiresIn=int(expires_seconds),
                )
            )

        return await asyncio.to_thread(_presign)

    async def verify_object_async(
        self, *, uri: str, expected_sha256: str, expected_bytes: int
    ) -> dict[str, str]:
        """Hash an existing object without persisting media on the function's disk."""
        ref = parse_s3_uri(uri)

        def verify() -> dict[str, str]:
            response = self._client.get_object(Bucket=ref.bucket, Key=ref.key)
            body = response["Body"]
            try:
                if response["ContentLength"] != expected_bytes:
                    raise ValueError("Recorded R2 object size changed")
                actual, size = sha256(), 0
                for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                    size += len(chunk)
                    if size > expected_bytes:
                        raise ValueError("Recorded R2 object exceeds the approved size")
                    actual.update(chunk)
                if size != expected_bytes or actual.hexdigest() != expected_sha256:
                    raise ValueError("Recorded R2 object hash changed")
                return {"etag": response["ETag"]}
            finally:
                body.close()

        return await asyncio.to_thread(verify)

    async def require_object_etag_async(self, *, uri: str, expected_etag: str) -> None:
        ref = parse_s3_uri(uri)
        response = await asyncio.to_thread(self._client.head_object, Bucket=ref.bucket, Key=ref.key)
        if response.get("ETag") != expected_etag:
            raise ValueError("Recorded R2 object identity changed")


@lru_cache(maxsize=1)
def get_s3_store() -> S3Store:
    return S3Store()


async def resolve_s3_uri_async(uri: str) -> str:
    """Resolve an s3:// URI to a presigned HTTPS URL for external consumers."""
    if not uri.startswith("s3://"):
        return uri
    store = get_s3_store()
    return await store.presign_get_async(
        uri=uri,
        expires_seconds=int(getattr(settings, "transcode_s3_presign_seconds", 86400) or 86400),
    )
