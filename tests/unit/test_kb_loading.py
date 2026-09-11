"""Tests for knowledge base and guardrails loading."""

from __future__ import annotations

import re

import pytest

from myloware.config.guardrails import get_guardrail_summary, load_guardrails
from myloware.knowledge import list_knowledge_documents, load_documents_with_manifest


def test_list_knowledge_documents():
    docs = list_knowledge_documents()
    assert len(docs) >= 3
    assert "editing/composition-and-audio" in docs
    assert "video-generation/wan-2.2-fast-prompting" in docs
    assert not any("veo3" in name or "tiktok-algorithm" in name for name in docs)


def test_load_documents_with_manifest_contains_content_and_metadata():
    docs, manifest = load_documents_with_manifest(project_id=None)
    assert docs, "Expected knowledge documents to load"
    assert manifest.get("hash")
    first = docs[0]
    assert first.id.startswith("kb_")
    assert first.filename.endswith(".md")
    assert len(first.content) > 50
    assert "kb_type" in first.metadata
    assert "document" in first.metadata
    assert first.metadata.get("chunk_index", 0) >= 0


def test_load_guardrails_projects():
    aismr = load_guardrails("aismr")
    motivational = load_guardrails("motivational")
    assert len(aismr) >= 5
    assert len(motivational) >= 1


def test_guardrail_summary_readable():
    summary = get_guardrail_summary("aismr")
    assert summary == "" or "Guardrails" in summary
    # If guardrails exist, expect some content
    if summary:
        assert len(summary) > 30


def test_guardrails_missing_project_returns_empty():
    assert load_guardrails("nonexistent_project") == {}
    assert get_guardrail_summary("nonexistent_project") == ""


def test_guardrail_summary_falls_back_to_str_for_non_dict(monkeypatch) -> None:
    from myloware.config import guardrails as guardrails_mod

    monkeypatch.setattr(guardrails_mod, "load_guardrails", lambda _p: {"x": ["a", "b"]})
    summary = guardrails_mod.get_guardrail_summary("p")
    assert "['a', 'b']" in summary


def test_active_documents_declare_roles_and_review_date():
    from myloware.knowledge.loader import load_knowledge_documents

    all_docs = list(load_knowledge_documents(project_id="aismr"))
    docs = [doc for doc in all_docs if doc.metadata["status"] == "active"]
    retired = next(doc for doc in all_docs if doc.filename == "zodiac-signs.md")
    assert retired.metadata["status"] == "legacy" and retired.metadata["roles"] == []
    assert docs
    assert {role for doc in docs for role in doc.metadata["roles"]} == {
        "ideator",
        "producer",
        "editor",
        "publisher",
        "supervisor",
    }
    for doc in docs:
        assert doc.metadata["status"] == "active"
        assert doc.metadata["roles"]
        assert doc.metadata.get("reviewed")
        assert not doc.content.startswith("---")


@pytest.mark.parametrize("project_id", ["aismr", "monthly"])
def test_active_aismr_knowledge_has_no_calendar_or_full_line_narration_contract(project_id):
    """Calendar wording belongs only to the renderer's opaque preset and bank."""
    from myloware.knowledge.loader import load_knowledge_documents

    active = [
        doc
        for doc in load_knowledge_documents(project_id=project_id)
        if doc.metadata["status"] == "active"
    ]
    forbidden = (
        "january through december",
        "month. label",
        "month-and-item",
        "month asset",
        "month assets",
        "spoken_text",
        "zodiac",
    )
    for document in active:
        content = document.content.casefold()
        assert not any(term in content for term in forbidden), document.filename
        if "ideator" in document.metadata["roles"]:
            assert not re.search(r"\b(months?|calendar|zodiac)\b", content), document.filename


def test_knowledge_front_matter_is_not_retrieved_text():
    from myloware.knowledge.loader import parse_document_metadata

    body, metadata = parse_document_metadata(
        "---\nroles: [producer]\nstatus: active\nreviewed: 2026-09-09\n---\n# Wan\nPrompt reference."
    )
    assert body == "# Wan\nPrompt reference."
    assert metadata == {"roles": ["producer"], "status": "active", "reviewed": "2026-09-09"}


def test_untagged_knowledge_remains_legacy():
    from myloware.knowledge.loader import parse_document_metadata

    body, metadata = parse_document_metadata("# Historical document")
    assert body == "# Historical document"
    assert metadata == {"roles": [], "status": "legacy"}


def test_invalid_knowledge_scope_fails_closed():
    import pytest

    from myloware.knowledge.loader import parse_document_metadata

    for header in (
        "roles: publisher",
        "roles: [admin]",
        "roles: []\nstatus: active",
        "roles: [producer]\nstatus: []",
    ):
        with pytest.raises(ValueError):
            parse_document_metadata(f"---\n{header}\n---\n# Body")


def test_knowledge_symlink_cannot_read_outside_scope(tmp_path):
    import pytest

    from myloware.knowledge.loader import _load_documents_from_dir

    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    outside = tmp_path / "private.md"
    outside.write_text("Private data")
    (knowledge / "linked.md").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        _load_documents_from_dir(knowledge)
