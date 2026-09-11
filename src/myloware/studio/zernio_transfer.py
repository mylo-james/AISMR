"""Exact-byte Zernio media presign transfer for the publication boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

from myloware.providers.zernio import ZERNIO_API_BASE_URL

_MAX_ZERNIO_MEDIA_BYTES = 5 * 1024 * 1024 * 1024
_PUBLIC_HOST = "media.zernio.com"
_PRESIGNED_UPLOAD_SUFFIX = ".r2.cloudflarestorage.com"


class ZernioMediaTransfer:
    """Upload an exact reviewed MP4 through Zernio's documented presign flow.

    The Zernio API key is used only for ``POST /media/presign``.  The returned
    storage URL receives the bytes with a content type header only.  We accept
    the currently documented R2 storage and Zernio public-media origins rather
    than following a provider-controlled arbitrary URL.
    """

    def __init__(self, presign_client: Any, upload_client: Any, *, api_key: Any) -> None:
        value = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else api_key
        if not isinstance(value, str) or not value:
            raise ValueError("Zernio media transfer requires an API key")
        self._presign_client = presign_client
        self._upload_client = upload_client
        self._api_key = value

    async def transfer(self, *, path: Path, sha256: str, operation_key: str) -> str:
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError("reviewed SHA-256 is required")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError("reviewed final artifact is unavailable") from exc
        if len(data) > _MAX_ZERNIO_MEDIA_BYTES or sha256 != hashlib.sha256(data).hexdigest():
            raise ValueError("final artifact bytes do not match the reviewed SHA-256")
        filename = f"aismr-{sha256[:16]}.mp4"
        request_id = str(uuid5(NAMESPACE_URL, f"aismr:zernio-media:{operation_key}"))
        response = await self._presign_client.post(
            f"{ZERNIO_API_BASE_URL}/media/presign",
            json={"filename": filename, "contentType": "video/mp4", "size": len(data)},
            headers={"Authorization": f"Bearer {self._api_key}", "x-request-id": request_id},
        )
        if not self._ok(response):
            raise RuntimeError("Zernio media presign outcome is unknown")
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError("Zernio media presign response is invalid") from exc
        if not isinstance(payload, dict):
            raise TypeError("Zernio media presign response is invalid")
        upload_url, public_url = payload.get("uploadUrl"), payload.get("publicUrl")
        if not self._allowed_upload_url(upload_url) or not self._allowed_public_url(public_url):
            raise RuntimeError("Zernio media presign response has an untrusted URL")
        upload = await self._upload_client.put(
            upload_url,
            content=data,
            headers={"Content-Type": "video/mp4"},
            follow_redirects=False,
        )
        if not self._ok(upload):
            raise RuntimeError("Zernio media upload outcome is unknown")
        return public_url

    @staticmethod
    def _ok(response: object) -> bool:
        status = getattr(response, "status_code", None)
        return isinstance(status, int) and 200 <= status < 300

    @staticmethod
    def _allowed_upload_url(value: object) -> bool:
        if not isinstance(value, str):
            return False
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and host.endswith(_PRESIGNED_UPLOAD_SUFFIX)
            and bool(host[: -len(_PRESIGNED_UPLOAD_SUFFIX)])
            and not parsed.username
            and not parsed.password
            and bool(parsed.query)
        )

    @staticmethod
    def _allowed_public_url(value: object) -> bool:
        if not isinstance(value, str):
            return False
        parsed = urlparse(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname == _PUBLIC_HOST
            and parsed.path.startswith("/temp/")
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
