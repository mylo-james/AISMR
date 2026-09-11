from __future__ import annotations


def test_build_editor_prompt_aismr_includes_objects_and_clips() -> None:
    from myloware.workflows.langgraph.prompts import build_editor_prompt

    prompt = build_editor_prompt(
        project="aismr",
        clip_urls=["https://example.com/a.mp4", "https://example.com/b.mp4"],
        creative_direction="ignored",
        overlays=[{"identifier": "Aries", "text": "Flame Spirit"}],
        duration_seconds=30.0,
    )

    assert "template: 'aismr'" in prompt
    assert "Flame Spirit" in prompt
    assert "https://example.com/a.mp4" in prompt


def test_build_editor_prompt_motivational_fills_missing_texts_for_non_mapping_overlays() -> None:
    from myloware.workflows.langgraph.prompts import build_editor_prompt

    prompt = build_editor_prompt(
        project="motivational",
        clip_urls=["https://example.com/a.mp4"],
        creative_direction="ignored",
        overlays=["not-a-mapping"],
        duration_seconds=30.0,
    )

    assert "template: 'motivational'" in prompt
    assert "TEXT 1" in prompt


def test_build_editor_prompt_motivational_includes_text_overlays() -> None:
    from myloware.workflows.langgraph.prompts import build_editor_prompt

    prompt = build_editor_prompt(
        project="motivational",
        clip_urls=["https://example.com/a.mp4", "https://example.com/b.mp4"],
        creative_direction="ignored",
        overlays=[{"text": "ONE"}, {"text": "TWO"}],
        duration_seconds=16.0,
    )

    assert "template: 'motivational'" in prompt
    assert "ONE" in prompt
    assert "TWO" in prompt
    assert "duration_seconds: 16.0" in prompt


def test_build_editor_prompt_default_uses_owned_renderer_and_supplied_duration() -> None:
    from myloware.workflows.langgraph.prompts import build_editor_prompt

    prompt = build_editor_prompt(
        project="unknown_project",
        clip_urls=["https://example.com/a.mp4"],
        creative_direction="direction",
        overlays=[],
        duration_seconds=12.5,
    )

    assert "analyze_media" not in prompt
    assert "remotion_render" in prompt
    assert "role_knowledge_search" in prompt
    assert "duration_seconds=12.5" in prompt
    assert "https://example.com/a.mp4" in prompt


def test_build_publisher_prompt_uses_project_template(monkeypatch) -> None:
    from myloware.workflows.langgraph.prompts import build_publisher_prompt
    from myloware.workflows.langgraph import prompts

    original_load_project = prompts.load_project

    def fake_load_project(_name: str):
        cfg = original_load_project("aismr")
        cfg.publisher_prompt_template = "Publish: {video_url} (topic={topic})"
        return cfg

    monkeypatch.setattr(prompts, "load_project", fake_load_project)

    prompt = build_publisher_prompt(
        project="aismr", video_url="https://example.com/v.mp4", topic="rain"
    )

    assert prompt.startswith("Publish: https://example.com/v.mp4 (topic=rain)")
    assert "CRITICAL: Call upload_post tool" in prompt


def test_build_publisher_prompt_falls_back_on_bad_template(monkeypatch) -> None:
    from myloware.workflows.langgraph.prompts import build_publisher_prompt
    from myloware.workflows.langgraph import prompts

    original_load_project = prompts.load_project

    def fake_load_project(_name: str):
        cfg = original_load_project("aismr")
        cfg.publisher_prompt_template = "Missing {placeholder}"
        return cfg

    monkeypatch.setattr(prompts, "load_project", fake_load_project)

    prompt = build_publisher_prompt(
        project="aismr", video_url="https://example.com/v.mp4", topic="rain"
    )

    assert "Publish this video" in prompt
    assert "https://example.com/v.mp4" in prompt
