"""Prompt builders for LangGraph workflow nodes.

Keeping prompt construction separate from orchestration logic makes the workflow
nodes easier to read and maintain, and keeps project-specific branching in one
place.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from myloware.config.projects import load_project

__all__ = [
    "build_editor_prompt",
    "build_publisher_prompt",
]


def build_editor_prompt(
    *,
    project: str,
    clip_urls: list[str],
    creative_direction: str,
    overlays: Any,
    duration_seconds: float,
) -> str:
    """Build the editor prompt for composing clips into a final video."""
    overlays_list: list[Any] = overlays if isinstance(overlays, list) else []

    if project == "aismr":
        objects: list[str] = []
        for item in overlays_list:
            if isinstance(item, Mapping):
                text = item.get("text") or item.get("identifier") or ""
                if text:
                    objects.append(str(text))
        return (
            f"Render an ASMR video with 12 segments (one per zodiac sign) using the 'aismr' template.\n\n"
            f"## CLIPS (use these EXACT URLs):\n{json.dumps(clip_urls, indent=2)}\n\n"
            f"## OBJECTS (use these EXACT names - NOT zodiac signs!):\n{json.dumps(objects, indent=2)}\n\n"
            f"CRITICAL: Call remotion_render tool with:\n"
            f"- template: 'aismr'\n"
            f"- clips: {json.dumps(clip_urls)}\n"
            f"- objects: {json.dumps(objects)}\n"
            f"- duration_seconds: {duration_seconds}\n"
            f"- fps: 30\n"
            f"- aspect_ratio: '9:16'\n\n"
            f"DO NOT just output code - you MUST call the remotion_render tool!"
        )

    if project == "motivational":
        texts_motivational: list[str] = []
        for idx, item in enumerate(overlays_list):
            if isinstance(item, Mapping):
                texts_motivational.append(
                    str(item.get("text") or item.get("identifier") or f"TEXT {idx + 1}")
                )
            else:
                texts_motivational.append(f"TEXT {idx + 1}")
        while len(texts_motivational) < 4:
            texts_motivational.append(f"TEXT {len(texts_motivational) + 1}")
        return (
            f"Render a motivational video with the 'motivational' template.\n\n"
            f"## CLIPS (use these EXACT URLs):\n{json.dumps(clip_urls, indent=2)}\n\n"
            f"## TEXT OVERLAYS (use these EXACT texts):\n{json.dumps(texts_motivational[:4], indent=2)}\n\n"
            f"CRITICAL: Call remotion_render tool with:\n"
            f"- template: 'motivational'\n"
            f"- clips: {json.dumps(clip_urls)}\n"
            f"- texts: {json.dumps(texts_motivational[:4])}\n"
            f"- duration_seconds: {duration_seconds}\n"
            f"- fps: 30\n"
            f"- aspect_ratio: '9:16'\n\n"
            f"DO NOT just output code - you MUST call the remotion_render tool!"
        )

    return (
        f"You have {len(clip_urls)} video clips to compose:\n"
        f"{json.dumps(clip_urls, indent=2)}\n\n"
        f"Creative direction/ideation:\n{creative_direction}\n\n"
        f"Compose only the supplied assets using a supported template or composition input.\n"
        f"Use role_knowledge_search if you need the renderer contract.\n"
        f"Preserve the provided labels and asset order. Do not generate replacement footage.\n"
        f"Call remotion_render with the supplied clips, duration_seconds={duration_seconds}, "
        f"fps=30 and aspect_ratio='9:16'.\n"
        f"DO NOT just output code - you MUST call the remotion_render tool!"
    )


def build_publisher_prompt(*, project: str, video_url: str, topic: str | None) -> str:
    """Build the publisher prompt.

    The project config supports an optional `publisher_prompt_template` that can
    include `{video_url}` and `{topic}` placeholders.
    """
    template = load_project(project).publisher_prompt_template
    if template:
        try:
            rendered = template.format(video_url=video_url, topic=topic or "")
        except (KeyError, ValueError):
            rendered = ""
        if rendered.strip():
            prompt = rendered.strip()
            prompt += (
                "\n\nCRITICAL: Call upload_post tool to publish this video. "
                "Include a caption under 150 characters and 3-8 tags."
            )
            return prompt

    prompt = f"Publish this video: {video_url}"
    if topic:
        prompt += f"\n\nTopic: {topic}"

    prompt += (
        "\n\nCRITICAL: Call upload_post tool to publish this video. "
        "Include a caption under 150 characters and 3-8 tags."
    )
    return prompt
