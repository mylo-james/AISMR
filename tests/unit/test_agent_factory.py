"""Unit tests for agent factory."""

from unittest.mock import Mock, patch

import pytest

from myloware.agents.factory import (
    create_file_search_tool_config,
    create_persona_agent,
    create_rag_tool_config,
)


def test_create_persona_agent_minimal():
    """Test creating agent with minimal required parameters."""
    mock_client = Mock()

    with patch("myloware.agents.factory.Agent") as mock_agent_class:
        mock_agent = Mock()
        mock_agent_class.return_value = mock_agent

        agent = create_persona_agent(
            client=mock_client,
            persona_name="test_persona",
            instructions="You are a test agent.",
        )

        mock_agent_class.assert_called_once()
        call_kwargs = mock_agent_class.call_args.kwargs

        assert call_kwargs["client"] == mock_client
        assert call_kwargs["instructions"] == "You are a test agent."
        assert call_kwargs["tools"] == []
        assert agent == mock_agent


def test_create_persona_agent_shields_not_passed_to_agent():
    """Test that shields are NOT passed to Agent (configured at server level)."""
    mock_client = Mock()

    with patch("myloware.agents.factory.Agent") as mock_agent_class:
        # Note: shields params are accepted by factory but not passed to Agent
        # The current llama_stack_client Agent class doesn't support shields
        create_persona_agent(
            client=mock_client,
            persona_name="test",
            instructions="Test instructions",
        )

        call_kwargs = mock_agent_class.call_args.kwargs
        # Shields are no longer passed to the Agent constructor
        # They are configured at the Llama Stack server level instead
        assert "input_shields" not in call_kwargs
        assert "output_shields" not in call_kwargs


def test_create_persona_agent_shield_params_accepted():
    """Test that shield params are accepted by factory even if not used."""
    mock_client = Mock()
    custom_shields = ["custom_shield"]

    with patch("myloware.agents.factory.Agent") as mock_agent_class:
        # Factory accepts these params for API compatibility, but doesn't use them
        create_persona_agent(
            client=mock_client,
            persona_name="test",
            instructions="Test",
            input_shields=custom_shields,
            output_shields=custom_shields,
        )

        call_kwargs = mock_agent_class.call_args.kwargs
        # Shields are not passed to Agent - configured at server level
        assert "input_shields" not in call_kwargs
        assert "output_shields" not in call_kwargs


def test_create_persona_agent_sampling_not_passed_to_agent():
    """Test that sampling_params is NOT passed to Agent (not supported)."""
    mock_client = Mock()

    with patch("myloware.agents.factory.Agent") as mock_agent_class:
        create_persona_agent(
            client=mock_client,
            persona_name="test",
            instructions="Test",
        )

        call_kwargs = mock_agent_class.call_args.kwargs
        # sampling_params is no longer passed to Agent constructor
        assert "sampling_params" not in call_kwargs


def test_create_persona_agent_with_tools():
    """Test creating agent with tools."""
    mock_client = Mock()
    mock_tool = {"name": "test_tool", "args": {}}

    with patch("myloware.agents.factory.Agent") as mock_agent_class:
        create_persona_agent(
            client=mock_client,
            persona_name="test",
            instructions="Test",
            tools=[mock_tool],
        )

        call_kwargs = mock_agent_class.call_args.kwargs
        assert call_kwargs["tools"] == [mock_tool]


def test_create_persona_agent_validates_persona_name():
    """Test that empty persona_name raises ValueError."""
    mock_client = Mock()

    with pytest.raises(ValueError, match="persona_name is required"):
        create_persona_agent(
            client=mock_client,
            persona_name="",
            instructions="Test",
        )


def test_create_persona_agent_validates_instructions():
    """Test that empty instructions raises ValueError."""
    mock_client = Mock()

    with pytest.raises(ValueError, match="instructions is required"):
        create_persona_agent(
            client=mock_client,
            persona_name="test",
            instructions="",
        )


def test_create_rag_tool_config_single_db():
    """Test RAG tool config with single DB ID."""
    config = create_rag_tool_config("my_kb")

    assert config["type"] == "file_search"
    assert config["vector_store_ids"] == ["my_kb"]


def test_create_rag_tool_config_multiple_dbs():
    """Test RAG tool config with multiple DB IDs."""
    config = create_rag_tool_config(["kb1", "kb2"])

    assert config["type"] == "file_search"
    assert config["vector_store_ids"] == ["kb1", "kb2"]


def test_create_file_search_tool_config_alias():
    config = create_file_search_tool_config("kb")
    assert config["type"] == "file_search"
    assert config["vector_store_ids"] == ["kb"]


def test_create_agent_fake_mode_returns_fake_agent(monkeypatch):
    from myloware.agents import factory

    monkeypatch.setattr(factory, "load_agent_config", lambda _p, _r: {"instructions": "test"})
    monkeypatch.setattr(factory, "effective_llama_stack_provider", lambda _s: "fake")
    monkeypatch.setattr(factory.settings, "environment", "development")

    agent = factory.create_agent(Mock(), "aismr", "ideator")
    assert hasattr(agent, "create_session")
    assert hasattr(agent, "create_turn")
    assert agent.create_session() == "session"
    resp = agent.create_turn("hello")
    assert hasattr(resp, "output_text")
    stream_iter = agent.create_turn("hello", stream=True)
    first = next(stream_iter)
    assert getattr(first, "response", None) is not None


def test_create_agent_off_mode_raises(monkeypatch):
    from myloware.agents import factory

    monkeypatch.setattr(factory, "load_agent_config", lambda _p, _r: {"instructions": "test"})
    monkeypatch.setattr(factory, "effective_llama_stack_provider", lambda _s: "off")
    monkeypatch.setattr(factory.settings, "environment", "development")

    with pytest.raises(RuntimeError, match="LLAMA_STACK_PROVIDER=off"):
        factory.create_agent(Mock(), "aismr", "ideator")


def test_create_agent_production_requires_real(monkeypatch):
    from myloware.agents import factory

    monkeypatch.setattr(factory, "load_agent_config", lambda _p, _r: {"instructions": "test"})
    monkeypatch.setattr(factory, "effective_llama_stack_provider", lambda _s: "fake")
    monkeypatch.setattr(factory.settings, "environment", "production")

    with pytest.raises(RuntimeError, match="LLAMA_STACK_PROVIDER!=real"):
        factory.create_agent(Mock(), "aismr", "ideator")


def test_create_agent_missing_instructions_raises(monkeypatch):
    from myloware.agents import factory

    monkeypatch.setattr(factory, "load_agent_config", lambda _p, _r: {})
    monkeypatch.setattr(factory, "effective_llama_stack_provider", lambda _s: "real")
    monkeypatch.setattr(factory.settings, "environment", "development")

    with pytest.raises(ValueError, match="No instructions found"):
        factory.create_agent(Mock(), "aismr", "ideator")


def test_create_agent_builds_tools_and_custom_tools(monkeypatch):
    from myloware.agents import factory

    monkeypatch.setattr(
        factory,
        "load_agent_config",
        lambda _p, _r: {"instructions": "test", "tools": ["builtin::rag/knowledge_search"]},
    )
    monkeypatch.setattr(factory, "effective_llama_stack_provider", lambda _s: "real")
    monkeypatch.setattr(factory.settings, "environment", "development")
    monkeypatch.setattr(factory, "_build_tools_from_config", lambda *_a, **_k: ["tool1"])

    with patch("myloware.agents.factory.Agent") as mock_agent_class:
        factory.create_agent(
            Mock(), "aismr", "test_persona", vector_db_id="kb", custom_tools=["tool2"]
        )
        call_kwargs = mock_agent_class.call_args.kwargs
        assert call_kwargs["tools"] == ["tool1", "tool2"]


def test_create_tool_instance_rag_and_builtin(monkeypatch):
    from myloware.agents import factory

    rag_tool = factory._create_tool_instance(
        "builtin::rag/knowledge_search", Mock(), "kb", run_id="r"
    )
    assert rag_tool["type"] == "file_search"

    monkeypatch.setattr(factory.settings, "brave_api_key", "key")
    web_tool = factory._create_tool_instance("builtin::websearch", Mock(), None, run_id="r")
    assert web_tool["type"] == "web_search"

    monkeypatch.setattr(factory.settings, "brave_api_key", "")
    assert factory._create_tool_instance("builtin::websearch", Mock(), None, run_id="r") is None


def test_build_tools_from_config_accepts_dict_tool():
    from myloware.agents import factory

    tool_config = {"type": "file_search", "vector_store_ids": ["kb"]}
    tools = factory._build_tools_from_config([tool_config], Mock(), None, run_id=None)
    assert tools == [tool_config]

    assert factory._create_tool_instance("builtin::unknown", Mock(), None, run_id="r") is None
    assert factory._create_tool_instance("custom_tool", Mock(), None, run_id="r") == "custom_tool"


def test_create_tool_instance_custom_tools(monkeypatch):
    from myloware.agents import factory

    sentinel = object()
    remotion_calls = []
    monkeypatch.setattr(factory, "SoraGenerationTool", lambda run_id=None: sentinel)
    monkeypatch.setattr(
        factory,
        "RemotionRenderTool",
        lambda run_id=None, project=None: remotion_calls.append((run_id, project)) or sentinel,
    )
    monkeypatch.setattr(factory, "UploadPostTool", lambda run_id=None: sentinel)
    monkeypatch.setattr(factory, "AnalyzeMediaTool", lambda run_id=None: sentinel)

    assert factory._create_tool_instance("sora_generate", Mock(), None, run_id="r") is sentinel
    assert (
        factory._create_tool_instance("remotion_render", Mock(), None, run_id="r", project="aismr")
        is sentinel
    )
    assert remotion_calls == [("r", "aismr")]
    assert factory._create_tool_instance("upload_post", Mock(), None, run_id="r") is sentinel
    assert factory._create_tool_instance("analyze_media", Mock(), None, run_id="r") is sentinel
