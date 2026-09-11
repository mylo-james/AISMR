"""Single-attempt fal submission, with the maintained SDK for read-only polling.

fal-client 1.0.1 retries POST transport failures internally, even with the
server-side X-Fal-No-Retry header. That cannot establish whether paid work was
accepted. Keep this narrow transport seam until the SDK exposes a no-retry
submission option; never patch its globals or pretend HTTP idempotency exists.
"""

import re
from dataclasses import dataclass
from typing import Any

import fal_client
import httpx


@dataclass(frozen=True)
class AcceptedHandle:
    request_id: str


class FalQueueClient:
    def __init__(self, *, key: str | None, transport: httpx.AsyncBaseTransport | None = None):
        if not key:
            raise ValueError("fal credentials are not configured")
        self._key = key
        self._transport = transport
        self._poll_client = httpx.AsyncClient(
            headers={"Authorization": f"Key {key}"},
            timeout=30,
            follow_redirects=False,
            transport=transport,
        )

    async def submit(
        self, application: str, *, arguments: dict[str, Any], headers: dict[str, str]
    ) -> AcceptedHandle:
        if application not in {
            "fal-ai/wan/v2.2-5b/text-to-video/fast-wan",
            "fal-ai/kokoro/american-english",
            "fal-ai/elevenlabs/tts/eleven-v3",
        }:
            raise ValueError("unconfigured fal application")
        # httpx does not retry requests by default. A lost response remains
        # uncertain for the durable workflow to reconcile, including HTTP 5xx.
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False, transport=self._transport
        ) as client:
            response = await client.post(
                f"https://queue.fal.run/{application}",
                json=arguments,
                headers={**headers, "Authorization": f"Key {self._key}"},
            )
            response.raise_for_status()
            data = response.json()
        request_id = data.get("request_id") if isinstance(data, dict) else None
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", request_id):
            raise RuntimeError("invalid fal submission receipt")
        return AcceptedHandle(request_id)

    async def get_handle(self, application: str, request_id: str) -> Any:
        if application not in {
            "fal-ai/wan/v2.2-5b/text-to-video/fast-wan",
            "fal-ai/kokoro/american-english",
            "fal-ai/elevenlabs/tts/eleven-v3",
        }:
            raise ValueError("unconfigured fal application")
        return fal_client.AsyncRequestHandle.from_request_id(
            self._poll_client, application, request_id
        )

    async def aclose(self) -> None:
        await self._poll_client.aclose()
