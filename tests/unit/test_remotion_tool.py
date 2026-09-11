"""Unit tests for Remotion render tool."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from myloware.tools.remotion import RemotionRenderTool


def test_remotion_tool_name_and_schema():
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"
        tool = RemotionRenderTool()

    assert tool.get_name() == "remotion_render"
    schema = tool.get_input_schema()
    props = schema["properties"]
    required = set(schema.get("required", []))
    assert "composition_code" in props
    assert "clips" in props
    assert "clips" in required


@pytest.mark.asyncio
async def test_remotion_tool_fake_mode():
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"
        tool = RemotionRenderTool(run_id="run-123")

    result = tool.run_impl(
        composition_code="export const RemotionComposition = () => null;",
        clips=["https://example.com/clip.mp4"],
    )

    assert result["success"] is True
    assert result["status"] == "queued"
    assert "job_id" in result


@pytest.mark.asyncio
async def test_remotion_tool_submits_payload():
    captured = {}

    async def capture_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs.get("json", {}))
        response = Mock()
        response.json.return_value = {"job_id": "job-123", "status": "queued"}
        response.raise_for_status = Mock()
        return response

    with (
        patch("myloware.tools.remotion.settings") as mock_settings,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.remotion_provider = "real"
        mock_settings.remotion_service_url = "http://render.local"
        mock_settings.webhook_base_url = "https://api.example.com"

        mock_client = AsyncMock()
        mock_client.post = capture_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        tool = RemotionRenderTool(run_id="abc")
        result = tool.run_impl(
            composition_code="export const RemotionComposition = () => null;",
            clips=["https://example.com/clip1.mp4"],
            duration_seconds=5,
            fps=30,
            aspect_ratio="9:16",
        )

    assert result["success"] is True
    assert captured["run_id"] == "abc"
    assert captured["callback_url"].startswith("https://api.example.com")
    assert captured["duration_frames"] == 150
    assert captured["width"] == 1080
    assert captured["height"] == 1920


@pytest.mark.asyncio
async def test_remotion_tool_infers_duration_for_template():
    captured = {}

    async def capture_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs.get("json", {}))
        response = Mock()
        response.json.return_value = {"job_id": "job-123", "status": "queued"}
        response.raise_for_status = Mock()
        return response

    with (
        patch("myloware.tools.remotion.settings") as mock_settings,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.remotion_provider = "real"
        mock_settings.remotion_service_url = "http://render.local"
        mock_settings.webhook_base_url = "https://api.example.com"

        mock_client = AsyncMock()
        mock_client.post = capture_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        tool = RemotionRenderTool(run_id="abc")
        result = tool.run_impl(
            template="motivational",
            clips=["https://example.com/clip1.mp4", "https://example.com/clip2.mp4"],
            fps=30,
            aspect_ratio="9:16",
            texts=["t1", "t2", "t3", "t4"],
        )

    assert result["success"] is True
    assert captured["duration_frames"] == 15 * 30


@pytest.mark.asyncio
async def test_remotion_monthly_submits_exact_service_contract() -> None:
    captured = {}

    async def capture_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs["json"])
        response = Mock()
        response.json.return_value = {"job_id": "job-monthly", "status": "queued"}
        response.raise_for_status = Mock()
        return response

    clips = [f"https://media.example/clips/{month}.mp4" for month in range(12)]
    labels = [f"Item {month}" for month in range(12)]
    narration_urls = [f"https://media.example/audio/{month}.mp3" for month in range(12)]
    with (
        patch("myloware.tools.remotion.settings") as mock_settings,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.remotion_provider = "real"
        mock_settings.remotion_service_url = "http://render.local"
        mock_settings.webhook_base_url = "https://api.example.com"
        mock_client = AsyncMock()
        mock_client.post = capture_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        result = RemotionRenderTool(run_id="run-monthly").run_impl(
            template="monthly",
            clips=clips,
            objects=labels,
            narration_urls=narration_urls,
            music_url="https://media.example/music.mp3",
            duration_seconds=74,
            fps=30,
        )

    assert result["status"] == "queued"
    assert captured["template"] == "monthly"
    assert captured["clips"] == clips
    assert captured["objects"] == labels
    assert captured["narration_urls"] == narration_urls
    assert captured["music_url"] == "https://media.example/music.mp3"
    assert "duration_frames" not in captured


@pytest.mark.asyncio
async def test_remotion_monthly_requires_portrait_aspect_ratio() -> None:
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"
        tool = RemotionRenderTool(run_id="monthly-run")

    with pytest.raises(ValueError, match="aspect_ratio=9:16"):
        tool.run_impl(
            template="monthly",
            clips=["https://media.example/clip.mp4"] * 12,
            objects=["item"] * 12,
            aspect_ratio="16:9",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clips", "objects", "narration_urls", "music_url", "message"),
    [
        (["https://media.example/clip.mp4"] * 11, ["item"] * 12, None, None, "exactly 12 HTTP"),
        (["https://media.example/clip.mp4"] * 12, ["item"] * 11, None, None, "non-empty item"),
        (
            ["https://media.example/clip.mp4"] * 12,
            ["item"] * 12,
            ["bad"] * 12,
            None,
            "narration_urls",
        ),
        (["https://media.example/clip.mp4"] * 12, ["item"] * 12, None, "bad", "music_url"),
        (["http://media.example/clip.mp4"] * 12, ["item"] * 12, None, None, "HTTP"),
        (["https://user:password@media.example/clip.mp4"] * 12, ["item"] * 12, None, None, "HTTP"),
    ],
)
async def test_remotion_monthly_rejects_invalid_inputs(
    clips, objects, narration_urls, music_url, message
) -> None:
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"
        tool = RemotionRenderTool(run_id="monthly-run")

    with pytest.raises(ValueError, match=message):
        tool.run_impl(
            template="monthly",
            clips=clips,
            objects=objects,
            narration_urls=narration_urls,
            music_url=music_url,
        )


@pytest.mark.asyncio
async def test_remotion_monthly_allows_loopback_http_urls() -> None:
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"
        tool = RemotionRenderTool(run_id="monthly-run")

    result = tool.run_impl(
        template="monthly",
        clips=["http://127.0.0.1:8080/clip.mp4"] * 12,
        objects=["item"] * 12,
    )
    assert result["status"] == "queued"


def test_remotion_rejects_unknown_arguments() -> None:
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"
        tool = RemotionRenderTool(run_id="run-123")

    with pytest.raises(TypeError, match="unexpected keyword"):
        tool.run_impl(
            composition_code="export const RemotionComposition = () => null;",
            clips=["https://example.com/clip.mp4"],
            arbitrary_audio="https://example.com/audio.mp3",
        )

    with pytest.raises(ValueError, match="only by the monthly"):
        tool.run_impl(
            template="aismr",
            clips=["https://example.com/clip.mp4"],
            narration_urls=["https://example.com/narration.mp3"],
        )


@pytest.mark.asyncio
async def test_remotion_template_rejects_non_30fps():
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"
        tool = RemotionRenderTool(run_id="abc")

    with pytest.raises(ValueError, match="fps=30"):
        tool.run_impl(
            template="aismr",
            clips=["https://example.com/clip1.mp4"],
            fps=24,
        )


@pytest.mark.asyncio
async def test_remotion_tool_requires_clips():
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"
        tool = RemotionRenderTool()

    with pytest.raises(ValueError):
        tool.run_impl(composition_code="export const RemotionComposition = () => null;", clips=[])


@pytest.mark.asyncio
async def test_remotion_tool_raises_http_error():
    with (
        patch("myloware.tools.remotion.settings") as mock_settings,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.remotion_provider = "real"
        mock_settings.remotion_service_url = "http://render.local"
        mock_settings.webhook_base_url = "https://api.example.com"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "fail", request=Mock(), response=Mock(status_code=500, text="error")
            )
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        tool = RemotionRenderTool(run_id="abc")
        with pytest.raises(httpx.HTTPStatusError):
            tool.run_impl(
                composition_code="export const RemotionComposition = () => null;",
                clips=["https://example.com/clip1.mp4"],
            )


def test_remotion_tool_rejects_off_provider():
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "off"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"
        with pytest.raises(ValueError, match="disabled"):
            RemotionRenderTool()


def test_remotion_tool_requires_service_url_in_real_mode():
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "real"
        mock_settings.remotion_service_url = ""
        mock_settings.webhook_base_url = "http://example.com"
        with pytest.raises(ValueError, match="REMOTION_SERVICE_URL"):
            RemotionRenderTool()


def test_remotion_tool_ignores_project_config_load_failure(monkeypatch):
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"

        def raise_load(_project):  # type: ignore[no-untyped-def]
            raise RuntimeError("nope")

        monkeypatch.setattr("myloware.config.projects.load_project", raise_load)
        tool = RemotionRenderTool(project="aismr")

    assert tool._object_validator_name is None


def test_remotion_tool_description_mentions_composition_code_when_allowed():
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"
        mock_settings.remotion_allow_composition_code = True
        mock_settings.remotion_sandbox_enabled = True
        tool = RemotionRenderTool()

    desc = tool.get_description()
    assert "composition_code" in desc


def test_infer_template_duration_unknown_template():
    with pytest.raises(ValueError, match="Unknown template"):
        RemotionRenderTool._infer_template_duration_seconds("wat")


def test_remotion_validate_result_requires_job_id():
    tool = RemotionRenderTool(run_id="x")
    with pytest.raises(ValueError, match="job_id"):
        tool._validate_result({})


@pytest.mark.asyncio
async def test_remotion_requires_template_or_composition_code():
    tool = RemotionRenderTool(run_id="x")
    with pytest.raises(ValueError, match="template"):
        tool.run_impl(clips=["https://example.com/a.mp4"])


@pytest.mark.asyncio
async def test_remotion_disallows_composition_code_without_sandbox():
    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "real"
        mock_settings.remotion_service_url = "http://render.local"
        mock_settings.webhook_base_url = "http://example.com"
        mock_settings.remotion_allow_composition_code = False
        mock_settings.remotion_sandbox_enabled = False
        tool = RemotionRenderTool()

    with pytest.raises(ValueError, match="composition_code is disabled"):
        tool.run_impl(
            composition_code="export const RemotionComposition = () => null;",
            clips=["https://example.com/clip.mp4"],
        )


@pytest.mark.asyncio
async def test_remotion_validates_objects_with_project_validator(monkeypatch):
    class FakeProject:
        object_validator = "my_validator"

    def fake_load(_project):  # type: ignore[no-untyped-def]
        return FakeProject()

    def fake_validate(_name, _objects):  # type: ignore[no-untyped-def]
        return False, "bad objects"

    monkeypatch.setattr("myloware.config.projects.load_project", fake_load)
    monkeypatch.setattr("myloware.workflows.validators.validate_objects", fake_validate)

    with patch("myloware.tools.remotion.settings") as mock_settings:
        mock_settings.remotion_provider = "fake"
        mock_settings.remotion_service_url = "http://localhost:3001"
        mock_settings.webhook_base_url = "http://example.com"
        tool = RemotionRenderTool(project="aismr")

    with pytest.raises(ValueError, match="bad objects"):
        tool.run_impl(
            template="aismr",
            clips=["https://example.com/clip.mp4"],
            objects=["obj1"] * 12,
        )
