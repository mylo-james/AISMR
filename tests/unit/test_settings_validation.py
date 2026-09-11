from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from myloware.config.settings import Settings


def test_validate_api_key_rejects_default_in_production_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENVIRONMENT", "production")

    with pytest.raises(ValidationError, match="Cannot use default API key in production"):
        Settings(environment="development", api_key="dev-api-key")


def test_sora_fake_clip_paths_validator_handles_empty_and_csv(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    s1 = Settings(sora_fake_clip_paths="")
    assert s1.sora_fake_clip_paths == []

    s2 = Settings(sora_fake_clip_paths=" a.mp4, b.mp4 , ,")
    assert s2.sora_fake_clip_paths == ["a.mp4", "b.mp4"]


def test_validate_transcode_storage_requires_bucket_when_s3(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="TRANSCODE_S3_BUCKET is required"):
        Settings(transcode_storage_backend="s3", transcode_s3_bucket="")


def test_validate_webhook_url_for_production_requires_base_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENVIRONMENT", "development")

    with pytest.raises(ValidationError, match="WEBHOOK_BASE_URL is required"):
        Settings(
            environment="production",
            webhook_base_url="",
            sora_provider="real",
            remotion_provider="real",
        )


def test_validate_model_requires_llama_stack_model(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="LLAMA_STACK_MODEL must be configured"):
        Settings(llama_stack_model="")


def test_force_content_shield_sets_default_when_blank(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    settings_obj = Settings(content_safety_shield_id="")
    assert settings_obj.content_safety_shield_id == "together/meta-llama/Llama-Guard-4-12B"


@pytest.mark.parametrize(
    "field", ["public_demo_allowed_workflows", "public_demo_cors_origins", "sora_fake_clip_paths"]
)
@pytest.mark.parametrize("source", ["env", "dotenv"])
@pytest.mark.parametrize(
    "value,expected",
    [
        (" one, two , ,", ["one", "two"]),
        ('["one", "two"]', ["one", "two"]),
        ("", []),
    ],
)
def test_list_settings_parse_real_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    source: str,
    value: str,
    expected: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(field.upper(), raising=False)
    if source == "env":
        monkeypatch.setenv(field.upper(), value)
    else:
        (tmp_path / ".env").write_text(f"{field.upper()}={value}\n")
    assert getattr(Settings(), field) == expected
