"""Focused tests for fixed product-role capabilities and local knowledge scope."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest
from llama_stack_client.lib.agents.types import CompletionMessage, ToolCall

from myloware.agents.roles import append_role_contract
from myloware.knowledge.loader import KnowledgeDocument
from myloware.tools.role_knowledge import RoleKnowledgeSearchTool


def _document(
    name: str, roles: list[str], content: str, status: str = "active"
) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=name,
        filename=f"{name}.md",
        content=content,
        metadata={
            "document": f"roles/{name}.md",
            "kb_type": "project:aismr",
            "roles": roles,
            "status": status,
        },
    )


@pytest.mark.asyncio
async def test_role_knowledge_filters_before_ranking_and_content_selection(monkeypatch) -> None:
    from myloware.tools import role_knowledge

    visible = _document("producer-guide", ["producer"], "# Producer guide\nwan prompt framing")
    hidden = _document("publisher-secret", ["publisher"], "# Publisher notes\nwan prompt secret")
    inactive = _document("producer-old", ["producer"], "# Old\nwan prompt", status="archived")
    monkeypatch.setattr(
        role_knowledge,
        "load_knowledge_documents",
        lambda **_kwargs: iter([hidden, inactive, visible]),
    )

    result = await RoleKnowledgeSearchTool(project="aismr", role="producer").async_run_impl(
        query="wan prompt"
    )

    assert [item["source"] for item in result["results"]] == ["roles/producer-guide.md"]
    assert result["results"][0]["roles"] == ["producer"]
    assert "secret" not in result["results"][0]["excerpt"]
    assert len(result["results"][0]["excerpt"]) <= 6000


@pytest.mark.asyncio
async def test_empty_role_knowledge_query_lists_only_authorized_titles(monkeypatch) -> None:
    from myloware.tools import role_knowledge

    monkeypatch.setattr(
        role_knowledge,
        "load_knowledge_documents",
        lambda **_kwargs: iter(
            [
                _document("producer", ["producer"], "# Producer reference\ncontent"),
                _document("editor", ["editor"], "# Editor reference\ncontent"),
            ]
        ),
    )

    result = await RoleKnowledgeSearchTool(project="aismr", role="producer").async_run_impl(
        query=""
    )

    assert result["results"] == [
        {
            "title": "Producer reference",
            "source": "roles/producer.md",
            "kb_type": "project:aismr",
            "roles": ["producer"],
        }
    ]


def test_role_knowledge_client_tool_serializes_execution(monkeypatch) -> None:
    from myloware.tools import role_knowledge

    monkeypatch.setattr(
        role_knowledge,
        "load_knowledge_documents",
        lambda **_kwargs: iter([_document("producer", ["producer"], "# Producer\nwan prompt")]),
    )
    message = CompletionMessage(
        role="assistant",
        content="",
        stop_reason="end_of_turn",
        tool_calls=[
            ToolCall(
                call_id="role-knowledge-call",
                tool_name="role_knowledge_search",
                arguments=json.dumps({"query": "wan"}),
            )
        ],
    )

    response = RoleKnowledgeSearchTool(project="aismr", role="producer").run([message])
    payload = json.loads(response.content)

    assert response.call_id == "role-knowledge-call"
    assert payload["success"] is True
    assert payload["results"][0]["source"] == "roles/producer.md"


@pytest.mark.parametrize(
    ("tools", "custom_tools"),
    [
        ([{"type": "file_search", "vector_store_ids": ["shared"]}], None),
        (["upload_post"], None),
        (["role_knowledge_search"], [Mock()]),
    ],
)
def test_product_role_rejects_config_and_runtime_tool_bypasses_before_agent(
    monkeypatch, tools, custom_tools
) -> None:
    from myloware.agents import factory

    monkeypatch.setattr(
        factory,
        "load_agent_config",
        lambda *_args: {"instructions": "project override", "tools": tools},
    )
    monkeypatch.setattr(factory, "effective_llama_stack_provider", lambda _settings: "fake")
    monkeypatch.setattr(factory.settings, "environment", "development")

    with (
        patch("myloware.agents.factory.Agent") as agent_constructor,
        pytest.raises((TypeError, ValueError), match="Role|Tool"),
    ):
        factory.create_agent(Mock(), "aismr", "ideator", custom_tools=custom_tools)
    agent_constructor.assert_not_called()


def test_product_role_tools_and_contract_are_fixed_after_project_override(monkeypatch) -> None:
    from myloware.agents import factory

    monkeypatch.setattr(
        factory,
        "load_agent_config",
        lambda *_args: {
            "instructions": "Project-specific instructions only.",
            "tools": ["builtin::rag/knowledge_search", "sora_generate"],
        },
    )
    monkeypatch.setattr(factory, "effective_llama_stack_provider", lambda _settings: "real")
    monkeypatch.setattr(factory.settings, "environment", "development")
    monkeypatch.setattr(factory, "SoraGenerationTool", lambda run_id=None: ("sora", run_id))

    with patch("myloware.agents.factory.Agent") as agent_constructor:
        factory.create_agent(Mock(), "aismr", "producer", run_id="run-1")

    kwargs = agent_constructor.call_args.kwargs
    assert [tool.get_name() if hasattr(tool, "get_name") else tool for tool in kwargs["tools"]] == [
        "role_knowledge_search",
        ("sora", "run-1"),
    ]
    assert kwargs["instructions"].endswith(append_role_contract("producer", "").strip())
    assert "You own only this stage: generate approved video clips." in kwargs["instructions"]


@pytest.mark.parametrize("project", ["../aismr", "/private/tmp", "aismr/../motivational", "..", ""])
def test_role_knowledge_rejects_project_paths_before_lookup(project, monkeypatch) -> None:
    from myloware.tools import role_knowledge

    reader = Mock()
    monkeypatch.setattr(role_knowledge, "load_knowledge_documents", reader)
    with pytest.raises(ValueError, match="project identifier"):
        RoleKnowledgeSearchTool(project=project, role="producer")
    reader.assert_not_called()


@pytest.mark.asyncio
async def test_role_knowledge_reads_only_fixed_project_and_role_from_disk(
    tmp_path, monkeypatch
) -> None:
    from myloware.knowledge import loader

    monkeypatch.setattr(loader, "ROOT", tmp_path)
    for project, role, marker in (
        ("aismr", "producer", "visible producer"),
        ("aismr", "publisher", "hidden publisher"),
        ("motivational", "producer", "hidden otherproject"),
    ):
        folder = tmp_path / "data" / "projects" / project / "knowledge"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{role}.md").write_text(
            f"---\nroles: [{role}]\nstatus: active\n---\n# {marker}\nwan prompt {marker}"
        )

    result = await RoleKnowledgeSearchTool(project="aismr", role="producer").async_run_impl("wan")
    assert len(result["results"]) == 1
    assert "visible producer" in result["results"][0]["excerpt"]
    assert "hidden" not in json.dumps(result)


@pytest.mark.parametrize("redirect", ["project", "knowledge"])
def test_project_knowledge_rejects_symlink_redirect(tmp_path, monkeypatch, redirect) -> None:
    from myloware.knowledge import loader

    monkeypatch.setattr(loader, "ROOT", tmp_path)
    projects = tmp_path / "data" / "projects"
    target = projects / "motivational" / "knowledge"
    target.mkdir(parents=True)
    if redirect == "project":
        (projects / "aismr").symlink_to(projects / "motivational", target_is_directory=True)
    else:
        (projects / "aismr").mkdir()
        (projects / "aismr" / "knowledge").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        RoleKnowledgeSearchTool(project="aismr", role="producer")
