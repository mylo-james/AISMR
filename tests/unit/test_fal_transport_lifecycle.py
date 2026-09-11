from __future__ import annotations

import httpx
import pytest

from myloware.providers.fal_transport import FalQueueClient


@pytest.mark.asyncio
async def test_poll_handle_uses_owned_no_redirect_key_client_and_closes() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "IN_QUEUE", "queue_position": 2})

    client = FalQueueClient(key="test-key", transport=httpx.MockTransport(respond))
    handle = await client.get_handle(
        "fal-ai/wan/v2.2-5b/text-to-video/fast-wan", "persisted-request"
    )
    status = await handle.status(with_logs=False)

    assert status.position == 2
    assert requests[0].headers["Authorization"] == "Key test-key"
    assert requests[0].url.host == "queue.fal.run"
    await client.aclose()
    assert client._poll_client.is_closed
