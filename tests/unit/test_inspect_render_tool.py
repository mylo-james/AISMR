"""Tests for editor-scoped render status inspection."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from llama_stack_client.lib.agents.types import CompletionMessage, ToolCall

from myloware.tools.inspect_render import InspectRenderTool


def _real_tool(run_id: str = "run-1") -> InspectRenderTool:
    with patch("myloware.tools.inspect_render.settings") as mock_settings:
        mock_settings.remotion_provider = "real"
        mock_settings.remotion_service_url = "http://render.local"
        mock_settings.remotion_api_secret = "secret"
        return InspectRenderTool(run_id=run_id)


def _http_client(payload: dict[str, object]) -> tuple[AsyncMock, Mock]:
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status = Mock()
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client, response


@pytest.mark.asyncio
async def test_inspect_render_returns_actual_done_status_and_bounded_diagnostics() -> None:
    tool = _real_tool()
    client, _response = _http_client(
        {
            "run_id": "run-1",
            "status": "done",
            "progress": 1,
            "template": "monthly",
            "output_url": "https://render.local/output/final.mp4",
            "video_metadata": {"fps": 30, "duration_seconds": 91.2, "path": "/private/path"},
            "media_diagnostics": {"verified": True, "audio": "present", "large": "x" * 600},
        }
    )
    with patch("myloware.tools.inspect_render.httpx.AsyncClient", return_value=client):
        result = await tool.async_run_impl("job-1")

    assert result["status"] == "done"
    assert result["final_artifact_url"].endswith("final.mp4")
    assert result["video_metadata"]["fps"] == 30
    assert len(result["media_diagnostics"]["large"]) == 500
    assert result["media_verified"] is True
    client.get.assert_awaited_once_with(
        "http://render.local/api/render/job-1",
        headers={"Authorization": "Bearer secret", "x-api-key": "secret"},
    )


@pytest.mark.asyncio
async def test_inspect_render_does_not_claim_pending_job_is_complete() -> None:
    tool = _real_tool()
    client, _response = _http_client(
        {"run_id": "run-1", "status": "rendering", "progress": 0.4, "output_url": "https://bad"}
    )
    with patch("myloware.tools.inspect_render.httpx.AsyncClient", return_value=client):
        result = await tool.async_run_impl("job-1")

    assert result["status"] == "rendering"
    assert "final_artifact_url" not in result
    assert result["media_verified"] is False


@pytest.mark.asyncio
async def test_inspect_render_failed_job_includes_bounded_source_diagnostics() -> None:
    tool = _real_tool()
    client, _response = _http_client(
        {
            "run_id": "run-1",
            "status": "error",
            "error": "narration 4 exceeds its clip duration",
            "media_diagnostics": {
                "verified": False,
                "errors": ["narration 4 exceeds its clip duration"],
                "input": {
                    "clips": [{"duration_seconds": 8.2, "has_video": True} for _ in range(13)],
                    "narration": [{"duration_seconds": 9.1, "has_audio": True}],
                },
            },
        }
    )
    with patch("myloware.tools.inspect_render.httpx.AsyncClient", return_value=client):
        result = await tool.async_run_impl("job-1")

    assert result["error"] == "narration 4 exceeds its clip duration"
    assert len(result["media_diagnostics"]["input"]["clips"]) == 12
    assert result["media_verified"] is False


@pytest.mark.asyncio
async def test_inspect_render_done_without_decode_verification_does_not_claim_verified() -> None:
    tool = _real_tool()
    client, _response = _http_client(
        {
            "run_id": "run-1",
            "status": "done",
            "output_url": "https://render.local/output/final.mp4",
            "media_diagnostics": {"verified": False},
        }
    )
    with patch("myloware.tools.inspect_render.httpx.AsyncClient", return_value=client):
        result = await tool.async_run_impl("job-1")

    assert result["final_artifact_url"].endswith("final.mp4")
    assert result["media_verified"] is False


@pytest.mark.asyncio
async def test_inspect_render_denies_cross_run_data() -> None:
    tool = _real_tool()
    client, _response = _http_client(
        {"run_id": "other-run", "status": "done", "output_url": "https://bad"}
    )
    with patch("myloware.tools.inspect_render.httpx.AsyncClient", return_value=client):
        result = await tool.async_run_impl("job-1")

    assert result["error_type"] == "render_job_run_mismatch"
    assert "output_url" not in result


@pytest.mark.asyncio
async def test_inspect_render_returns_structured_transient_error() -> None:
    tool = _real_tool()
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("myloware.tools.inspect_render.httpx.AsyncClient", return_value=client):
        result = await tool.async_run_impl("job-1")

    assert result["error_type"] == "render_status_unavailable"


def test_inspect_render_fake_mode_does_not_claim_verified_media() -> None:
    with patch("myloware.tools.inspect_render.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://render.local"
        tool = InspectRenderTool(run_id=None)

    result = tool.run_impl(job_id="fake-job")
    assert result["status"] == "fake_unavailable"
    assert result["media_verified"] is False


def test_inspect_render_client_tool_serializes_status(monkeypatch) -> None:
    tool = _real_tool()

    async def status(*, job_id: str):
        assert job_id == "job-1"
        return {"success": True, "job_id": "job-1", "status": "queued"}

    monkeypatch.setattr(tool, "async_run_impl", status)
    message = CompletionMessage(
        role="assistant",
        content="",
        stop_reason="end_of_turn",
        tool_calls=[
            ToolCall(
                call_id="inspect-call",
                tool_name="inspect_render",
                arguments=json.dumps({"job_id": "job-1"}),
            )
        ],
    )

    response = tool.run([message])
    assert response.call_id == "inspect-call"
    assert json.loads(response.content)["status"] == "queued"
