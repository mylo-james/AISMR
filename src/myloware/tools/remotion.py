"""Remotion video rendering tool for Llama Stack agents.

Submits compositions to the self-hosted Remotion render service.
Supports both custom TSX code and pre-built templates.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from opentelemetry import trace

from myloware.config import settings
from myloware.config.provider_modes import effective_remotion_provider
from myloware.observability.logging import get_logger
from myloware.tools.base import JSONSchema, MylowareBaseTool, format_tool_success

logger = get_logger(__name__)
tracer = trace.get_tracer("myloware.tools.remotion")

__all__ = ["RemotionRenderTool"]

_MONTHLY_ITEM_COUNT = 12


def _is_http_url(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value)
    if parsed.username or parsed.password or not parsed.hostname:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}


class RemotionRenderTool(MylowareBaseTool):
    """Render video compositions using self-hosted Remotion service.

    Supports two modes:
    1. Template mode: Use a pre-built template (e.g., "aismr") with data
    2. Custom mode: Provide React/TSX composition code directly
    """

    def __init__(
        self,
        run_id: str | None = None,
        timeout: float = 30.0,
        project: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.timeout = timeout
        self.project = project
        self.base_url = getattr(settings, "remotion_service_url", "http://localhost:3001")
        webhook_base = getattr(settings, "webhook_base_url", "")
        self.callback_url = (
            f"{webhook_base}/v1/webhooks/remotion?run_id={run_id}"
            if webhook_base and run_id
            else None
        )
        self.provider_mode = effective_remotion_provider(settings)
        if self.provider_mode == "off":
            raise ValueError("Remotion provider is disabled (REMOTION_PROVIDER=off)")
        self.use_fake = self.provider_mode == "fake"
        self.api_secret = getattr(settings, "remotion_api_secret", "")
        self.allow_composition_code = getattr(settings, "remotion_allow_composition_code", False)
        self.sandbox_enabled = getattr(settings, "remotion_sandbox_enabled", False)
        self.sandbox_strict = getattr(settings, "remotion_sandbox_strict", False)

        # Load project-specific validator if project is specified
        self._object_validator_name: str | None = None
        if project:
            try:
                from myloware.config.projects import load_project

                project_config = load_project(project)
                self._object_validator_name = project_config.object_validator
            except Exception as e:
                logger.debug("Could not load project config for validation: %s", e)

        if self.provider_mode == "real" and not self.base_url:
            raise ValueError("REMOTION_SERVICE_URL must be configured")

    def get_name(self) -> str:
        return "remotion_render"

    def get_description(self) -> str:
        desc = (
            "Render a video using Remotion. "
            "Templates: 'aismr' (12 zodiac clips + objects), 'motivational' (2 clips + 4 text overlays), "
            "and 'monthly' (12 January through December clips and item labels). "
        )
        if self.allow_composition_code:
            desc += (
                "OR write custom React/TSX composition_code for full creative control. "
                "Query knowledge base for Remotion API, components, animations. "
            )
        else:
            desc += "For custom compositions, enable REMOTION_ALLOW_COMPOSITION_CODE=true."
        return desc

    def get_input_schema(self) -> JSONSchema:
        return {
            "type": "object",
            "properties": {
                "clips": {
                    "type": "array",
                    "description": "Array of video clip URLs in order",
                    "items": {"type": "string"},
                },
                "template": {
                    "type": "string",
                    "description": "Template name (e.g., 'aismr'). Use instead of composition_code.",
                },
                "composition_code": {
                    "type": "string",
                    "description": "Custom React/TSX code (used if no template specified)",
                },
                "objects": {
                    "type": "array",
                    "description": (
                        "REQUIRED for AISMR template. Array of 12 CREATIVE OBJECT NAMES "
                        "(e.g., 'Flame Spirit', 'Earth Golem', 'Shadow Serpent') - "
                        "NOT zodiac signs! These are displayed as text overlays in the video."
                    ),
                    "items": {"type": "string"},
                },
                "texts": {
                    "type": "array",
                    "description": (
                        "REQUIRED for MOTIVATIONAL template. Array of 4 text overlays "
                        "displayed sequentially (4 seconds each). Extract from ideation."
                    ),
                    "items": {"type": "string"},
                },
                "narration_urls": {
                    "type": "array",
                    "description": "Optional monthly narration URLs, exactly one for each of 12 clips.",
                    "items": {"type": "string"},
                },
                "music_url": {
                    "type": "string",
                    "description": "Optional monthly background music URL.",
                },
                "edit_plan": {
                    "type": "object",
                    "description": "Optional monthly-only edit plan. Exactly 12 values per supplied array.",
                    "properties": {
                        "segment_frames": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1, "maximum": 1800},
                        },
                        "clip_playback_rates": {
                            "type": "array",
                            "items": {"type": "number", "exclusiveMinimum": 0, "maximum": 5},
                        },
                        "fade_frames": {"type": "integer", "minimum": 0, "maximum": 900},
                        "narration_start_frames": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0},
                        },
                        "scene_effects": {
                            "type": "array",
                            "minItems": 12,
                            "maxItems": 12,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "zoomStart": {"type": "number", "minimum": 1, "maximum": 1.18},
                                    "zoomEnd": {"type": "number", "minimum": 1, "maximum": 1.18},
                                    **{
                                        key: {"type": "number", "minimum": -0.03, "maximum": 0.03}
                                        for key in ("panXStart", "panXEnd", "panYStart", "panYEnd")
                                    },
                                    "saturation": {
                                        "type": "number",
                                        "minimum": 0.8,
                                        "maximum": 1.15,
                                    },
                                    "contrast": {"type": "number", "minimum": 0.9, "maximum": 1.1},
                                    "vignette": {"type": "number", "minimum": 0, "maximum": 0.22},
                                },
                            },
                        },
                        **{
                            key: {"type": "number", "minimum": 0, "maximum": 1}
                            for key in ("narration_volume", "music_volume", "music_ducked_volume")
                        },
                    },
                },
                "duration_seconds": {
                    "type": "number",
                    "description": (
                        "Total video duration in seconds. If omitted and using a known template, "
                        "duration is inferred from the template timeline (e.g. aismr=74s, motivational=15s). "
                        "If using composition_code, you must provide duration_seconds."
                    ),
                },
                "fps": {
                    "type": "integer",
                    "description": (
                        "Frames per second (24, 30, or 60). "
                        "Template mode is fixed at 30fps to match timeline constants."
                    ),
                    "default": 30,
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Video aspect ratio: '9:16', '16:9', or '1:1'",
                    "default": "9:16",
                },
            },
            "required": ["clips"],
        }

    @staticmethod
    def _infer_template_duration_seconds(template: str) -> float:
        key = template.strip().lower()
        if key == "aismr":
            return 74.0
        if key == "motivational":
            # 2x 8s clips with 1s overlap -> 15s total
            return 15.0
        raise ValueError(f"Unknown template: {template}")

    @staticmethod
    def _validate_monthly_inputs(
        clips: List[str],
        objects: Optional[List[str]],
        narration_urls: Optional[List[str]],
        music_url: Optional[str],
        fps: int,
        aspect_ratio: str,
    ) -> None:
        if fps != 30:
            raise ValueError("Monthly template requires fps=30")
        if aspect_ratio != "9:16":
            raise ValueError("Monthly template requires aspect_ratio=9:16")
        if len(clips) != _MONTHLY_ITEM_COUNT or not all(_is_http_url(clip) for clip in clips):
            raise ValueError("Monthly template requires exactly 12 HTTP(S) clip URLs")
        if (
            objects is None
            or len(objects) != _MONTHLY_ITEM_COUNT
            or not all(isinstance(label, str) and label.strip() for label in objects)
        ):
            raise ValueError("Monthly template requires exactly 12 non-empty item labels")
        if narration_urls is not None and (
            len(narration_urls) != _MONTHLY_ITEM_COUNT
            or not all(_is_http_url(url) for url in narration_urls)
        ):
            raise ValueError("Monthly narration_urls must contain exactly 12 HTTP(S) URLs")
        if music_url is not None and not _is_http_url(music_url):
            raise ValueError("Monthly music_url must be an HTTP(S) URL")

    def _validate_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Validate render submission response."""
        if not result.get("job_id"):
            raise ValueError("job_id is required in Remotion result")
        return result

    # run_impl is handled by MylowareBaseTool which wraps async_run_impl

    async def async_run_impl(
        self,
        clips: list[str],
        template: str | None = None,
        composition_code: str | None = None,
        objects: list[str] | None = None,
        texts: list[str] | None = None,
        narration_urls: list[str] | None = None,
        music_url: str | None = None,
        edit_plan: dict[str, Any] | None = None,
        duration_seconds: float | None = None,
        fps: int = 30,
        aspect_ratio: str = "9:16",
    ) -> dict[str, Any]:
        """Submit composition to Remotion render service."""
        if not clips:
            raise ValueError("At least one clip URL is required")

        if not template and not composition_code:
            raise ValueError("Either 'template' or 'composition_code' must be provided")

        if template and fps != 30:
            raise ValueError("Template renders require fps=30 to match fixed timelines")

        template_key = template.strip().lower() if template else None
        if template_key == "monthly":
            self._validate_monthly_inputs(
                clips, objects, narration_urls, music_url, fps, aspect_ratio
            )
            if edit_plan is not None and (
                not isinstance(edit_plan, dict)
                or set(edit_plan)
                - {
                    "segment_frames",
                    "clip_playback_rates",
                    "fade_frames",
                    "narration_start_frames",
                    "scene_effects",
                    "narration_volume",
                    "music_volume",
                    "music_ducked_volume",
                }
            ):
                raise ValueError("Monthly edit_plan is invalid")
        elif narration_urls is not None or music_url is not None or edit_plan is not None:
            raise ValueError(
                "narration_urls and music_url are supported only by the monthly template"
            )

        if (
            composition_code
            and not self.use_fake
            and not (self.allow_composition_code and self.sandbox_enabled and self.sandbox_strict)
        ):
            raise ValueError(
                "composition_code is disabled. Enable strict sandboxing + allowlist or use a template."
            )

        # Validate objects using project-specific validator (if configured)
        if objects and self._object_validator_name:
            from myloware.workflows.validators import validate_objects

            is_valid, error_msg = validate_objects(self._object_validator_name, objects)
            if not is_valid:
                raise ValueError(error_msg)

        dimensions = {
            "9:16": (1080, 1920),
            "16:9": (1920, 1080),
            "1:1": (1080, 1080),
        }
        width, height = dimensions.get(aspect_ratio, (1080, 1920))

        inferred_seconds: float | None = None
        if template and template_key != "monthly":
            inferred_seconds = self._infer_template_duration_seconds(template)

        # Template mode: the template owns the timeline. Ignore caller-provided duration
        # to avoid accidental truncation or black tail padding.
        if inferred_seconds is not None:
            duration_seconds_effective = inferred_seconds
        else:
            duration_seconds_effective = duration_seconds if duration_seconds is not None else 10.0

        duration_frames = (
            int(duration_seconds_effective * fps) if template_key != "monthly" else None
        )

        if self.use_fake:
            logger.info(
                "Fake Remotion render submission (run_id=%s, template=%s)", self.run_id, template
            )
            return format_tool_success(
                data={
                    "job_id": f"fake-remotion-{self.run_id or '0000'}",
                    "status": "queued",
                    "template": template,
                    **(
                        {
                            "estimated_duration_seconds": duration_seconds_effective,
                            "duration_frames": duration_frames,
                        }
                        if template_key != "monthly"
                        else {}
                    ),
                },
                message="Render job queued (fake mode)",
            )

        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "clips": clips,
            "fps": fps,
            "width": width,
            "height": height,
            "callback_url": self.callback_url,
        }
        if duration_frames is not None:
            payload["duration_frames"] = duration_frames

        # Add template or composition_code
        if template:
            payload["template"] = template
            if objects:
                payload["objects"] = objects
            if texts:
                payload["texts"] = texts
            if narration_urls is not None:
                payload["narration_urls"] = narration_urls
            if music_url is not None:
                payload["music_url"] = music_url
            if edit_plan is not None:
                payload["edit_plan"] = edit_plan
        else:
            payload["composition_code"] = composition_code

        headers = {}
        if self.api_secret:
            headers["Authorization"] = f"Bearer {self.api_secret}"
            headers["x-api-key"] = self.api_secret

        with tracer.start_as_current_span(
            "remotion_render",
            attributes={
                "service.url": self.base_url,
                "has_template": bool(template),
                "has_composition_code": bool(composition_code),
                "run_id": self.run_id or "",
            },
        ):
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/render", json=payload, headers=headers
                )
                response.raise_for_status()
                result = response.json()

        logger.info(
            "Remotion render job submitted: %s (template=%s)", result.get("job_id"), template
        )

        return format_tool_success(
            data={
                "job_id": result.get("job_id"),
                "status": result.get("status"),
                "template": template,
                "service": "remotion",
            },
            message="Render job submitted to Remotion service"
            + (f" using {template} template" if template else ""),
        )
