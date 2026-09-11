"""Agent factory for creating Llama Stack persona agents.

## Llama Stack 0.3.x Changes
Supports two modes:
1. Direct creation with explicit instructions (create_persona_agent)
2. Config-driven creation from YAML files (create_agent)

Tools are created per-agent with run context (run_id) for webhook callbacks.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List
from unittest.mock import MagicMock
from uuid import UUID

from llama_stack_client import LlamaStackClient
from llama_stack_client.lib.agents.agent import Agent
from llama_stack_client.lib.agents.client_tool import ClientTool

from myloware.agents.roles import (
    append_role_contract,
    is_product_role,
    validate_role_tool_configuration,
)
from myloware.config import settings
from myloware.config.loaders import load_agent_config
from myloware.config.provider_modes import effective_llama_stack_provider
from myloware.observability.logging import get_logger
from myloware.tools import AnalyzeMediaTool, RemotionRenderTool, SoraGenerationTool, UploadPostTool
from myloware.tools.inspect_render import InspectRenderTool
from myloware.tools.role_knowledge import RoleKnowledgeSearchTool

logger = get_logger(__name__)

__all__ = [
    "create_agent",
    "create_persona_agent",
    "create_rag_tool_config",
]


def create_persona_agent(
    client: LlamaStackClient,
    persona_name: str,
    instructions: str,
    tools: List[Any] | None = None,
    model_id: str | None = None,
    input_shields: List[str] | None = None,  # accepted for API-compat, enforced upstream
    output_shields: List[str] | None = None,  # accepted for API-compat, enforced upstream
) -> Agent:
    """
    Create a Llama Stack Agent for a persona.

    This factory ensures consistent configuration across all persona agents.

    Note: In 0.3.x, safety shields are NOT built into Agent.
    Use client.safety.run_shield() for content moderation.
    """
    if not persona_name:
        raise ValueError("persona_name is required")
    if not instructions:
        raise ValueError("instructions is required")

    model = model_id or settings.llama_stack_model
    agent_tools = tools or []

    logger.info(
        "Creating persona agent: name=%s, model=%s, tools=%d",
        persona_name,
        model,
        len(agent_tools),
    )

    # Llama Stack 0.3.0 Agent ctor does not accept shields; safety is enforced
    # via HTTP middleware and pre-flight shield calls on briefs.
    agent = Agent(
        client=client,
        model=model,
        instructions=instructions,
        tools=agent_tools,
    )

    logger.info("Created agent for persona '%s'", persona_name)
    return agent


def create_rag_tool_config(vector_db_ids: List[str] | str) -> Dict[str, Any]:
    """
    Create a Llama Stack RAG/file_search tool configuration.

    Uses the OpenAI-compatible file_search format which is required by
    the Responses API in Llama Stack 0.3.x. Supports:
    - Vector similarity search
    - Hybrid search (via ranking_options)
    - Auto-chunking and embedding

    Args:
        vector_db_ids: Vector store ID(s) to search

    Returns:
        Tool configuration dict with type discriminator for Responses API
    """
    if isinstance(vector_db_ids, str):
        vector_db_ids = [vector_db_ids]

    # Responses API requires type discriminator - use file_search format
    return {
        "type": "file_search",
        "vector_store_ids": vector_db_ids,
    }


def create_file_search_tool_config(vector_store_ids: List[str] | str) -> Dict[str, Any]:
    """Create file_search tool config. Alias for create_rag_tool_config."""
    return create_rag_tool_config(vector_store_ids)


def create_agent(
    client: LlamaStackClient,
    project: str,
    role: str,
    vector_db_id: str | None = None,
    run_id: UUID | str | None = None,
    custom_tools: List[Any] | None = None,
    input_shields: List[str] | None = None,  # accepted for API-compat, enforced upstream
    output_shields: List[str] | None = None,  # accepted for API-compat, enforced upstream
) -> Agent:
    """Create a Llama Stack agent from project config.

    Loads agent configuration from YAML files with inheritance:
    - Base config from data/shared/agents/{role}.yaml
    - Project override from data/projects/{project}/agents/{role}.yaml (if exists)

    Args:
        client: Llama Stack client
        project: Project name (e.g., "aismr", "motivational")
        role: Agent role (ideator, producer, editor, publisher, supervisor)
        vector_db_id: Optional vector DB ID for RAG tools
        run_id: Optional run ID for webhook-enabled tools (Sora, Remotion)
        custom_tools: Optional additional tools to add

    Returns:
        Configured Llama Stack Agent

    Note:
        In 0.3.x, safety shields are NOT built into Agent.
        YAML config fields 'input_shields' and 'output_shields' are ignored.
        Use client.safety.run_shield() for content moderation.
    """
    # Load config with inheritance
    config = load_agent_config(project, role)
    validate_role_tool_configuration(role, config.get("tools", []), custom_tools)

    instructions = config.get("instructions", "")
    if not instructions:
        raise ValueError(f"No instructions found in config for {role}")
    instructions = append_role_contract(role, instructions)

    logger.info("Creating %s agent for project %s (run_id=%s)", role, project, run_id)

    llama_mode = effective_llama_stack_provider(settings)

    if settings.environment == "production" and llama_mode != "real":
        raise RuntimeError(
            "Cannot use LLAMA_STACK_PROVIDER!=real in production. "
            "Set LLAMA_STACK_PROVIDER=real or ENVIRONMENT != production"
        )

    if llama_mode == "off":
        raise RuntimeError("LLAMA_STACK_PROVIDER=off: Llama Stack is disabled (fail-fast)")

    if llama_mode == "fake" and not isinstance(Agent, MagicMock):

        class _FakeAgent:
            def __init__(self) -> None:
                self._last_session = None

            def create_session(self, session_name: str | None = None) -> str:
                self._last_session = session_name or "session"
                return self._last_session

            def create_turn(self, *args: Any, **kwargs: Any) -> Any:
                class Resp:
                    def __init__(self, text: str) -> None:
                        self.output_text = text

                resp = Resp(
                    "AISMR is a Llama Stack native video production pipeline "
                    "built with FastAPI and Python."
                )

                if kwargs.get("stream"):
                    # Minimal streaming shape: yield a single chunk that carries the final response.
                    def _iter():
                        yield type("Chunk", (), {"event": None, "response": resp})()

                    return _iter()

                return resp

        return _FakeAgent()

    # Build tools list from config - tools are created per-run with context
    tools = _build_tools_from_config(
        config.get("tools", []),
        client=client,
        vector_db_id=vector_db_id,
        run_id=run_id,
        project=project,
        role=role,
    )

    for idx, tool in enumerate(tools):
        logger.info(
            "Tool #%s type=%s module=%s callable=%s is_client_tool=%s",
            idx,
            type(tool),
            getattr(tool, "__module__", None),
            callable(tool),
            isinstance(tool, ClientTool),
        )

    # Custom tools are available only to non-product personas. Product roles are
    # rejected above before a provider-specific Agent is instantiated.
    if custom_tools:
        tools.extend(custom_tools)

    # Get model from config or settings
    # YAML agent configs may pin models (e.g., openai/*), but allow a global override via env
    # for local debugging or quota outages without editing agent configs.
    model_override = os.getenv("LLAMA_STACK_MODEL")
    model = (
        settings.llama_stack_model
        if model_override
        else (config.get("model") or settings.llama_stack_model)
    )

    # Llama Stack 0.3.0 Agent ctor does not accept shields; safety handled
    # upstream (middleware + pre-flight shields).
    agent = Agent(
        client=client,
        model=model,
        instructions=instructions,
        tools=tools,
    )

    logger.info("Created %s agent with %d tools", role, len(tools))
    return agent


def _build_tools_from_config(
    tool_names: List[str | Dict[str, Any]],
    client: LlamaStackClient,
    vector_db_id: str | None,
    run_id: UUID | str | None = None,
    project: str | None = None,
    role: str | None = None,
) -> List[Any]:
    """Build tool list from config, injecting context where needed.

    Tools are created fresh per-agent with full context (run_id, vector_db_id).
    This is the Llama Stack native approach - no global caching of stateful tools.

    Handles:
    - builtin:: tools (RAG with hybrid search, websearch)
    - Custom AISMR tools (sora_generate, remotion_render, upload_post)

    Args:
        tool_names: List of tool names or tool configs from YAML
        client: Llama Stack client for creating hybrid RAG tools
        vector_db_id: Optional vector DB ID to inject into RAG tools
        run_id: Optional run ID for webhook-enabled tools

    Returns:
        List of tool configurations (strings for builtin, instances for custom)
    """
    tools: List[Any] = []

    # Convert run_id to string if it's a UUID
    run_id_str = str(run_id) if run_id else None

    for tool in tool_names:
        if isinstance(tool, str):
            # String tool name
            tool_instance = _create_tool_instance(
                tool, client, vector_db_id, run_id_str, project=project, role=role
            )
            if tool_instance is not None:
                tools.append(tool_instance)
        elif isinstance(tool, dict):
            # Dict tool config - pass through as-is
            # The tool may already be in the new format (type: file_search)
            tools.append(tool)

    return tools


def _create_tool_instance(
    tool_name: str,
    client: LlamaStackClient,
    vector_db_id: str | None,
    run_id: str | None = None,
    project: str | None = None,
    role: str | None = None,
) -> Any:
    """Create a tool instance from a tool name.

    Custom tools (Sora, Remotion, etc.) are created fresh each time with
    run context. This enables webhook callbacks to include run_id.

    Returns:
        - Tool instance for custom tools
        - Dict config for builtin tools that need args
        - String for simple builtin tools
        - None if tool should be skipped
    """
    # RAG/file_search tool - uses OpenAI-compatible file_search format
    if tool_name in {"builtin::rag/knowledge_search", "role_knowledge_search"}:
        if role and project and is_product_role(role):
            return RoleKnowledgeSearchTool(project=project, role=role)
        if vector_db_id:
            tool_config = create_rag_tool_config(vector_db_id)
            logger.info("Created file_search tool (vector_store_ids=%s)", vector_db_id)
            return tool_config
        raise RuntimeError("RAG tool requested but no vector_db_id provided (fail-fast)")

    # Websearch tool - OpenAI-compatible format
    if tool_name == "builtin::websearch":
        if settings.brave_api_key:
            logger.info("Configured web_search tool")
            return {"type": "web_search"}
        logger.info("Skipping web_search tool (BRAVE_API_KEY not set)")
        return None

    # Other builtin tools - skip (not supported in new API)
    if tool_name.startswith("builtin::"):
        logger.warning("Unsupported builtin tool '%s', skipping", tool_name)
        return None

    # Custom AISMR tools - create fresh instances with run context
    # These are NOT cached because they may contain run-specific state

    if tool_name == "sora_generate":
        tool_instance = SoraGenerationTool(run_id=run_id)
        # Instrumentation: verify the tool instance
        from myloware.tools.base import MylowareBaseTool

        is_our_tool = isinstance(tool_instance, MylowareBaseTool)
        logger.info(
            "Created SoraGenerationTool (run_id=%s, type=%s, is MylowareBaseTool=%s, MRO=%s)",
            run_id,
            type(tool_instance),
            is_our_tool,
            tool_instance.__class__.mro(),
        )
        if not is_our_tool:
            logger.error("BUG: SoraGenerationTool is not a MylowareBaseTool instance!")
        return tool_instance

    if tool_name == "remotion_render":
        tool_instance = RemotionRenderTool(run_id=run_id, project=project)
        logger.info("Created RemotionRenderTool (run_id=%s)", run_id)
        return tool_instance

    if tool_name == "inspect_render":
        tool_instance = InspectRenderTool(run_id=run_id)
        logger.info("Created InspectRenderTool (run_id=%s)", run_id)
        return tool_instance

    if tool_name == "upload_post":
        tool_instance = UploadPostTool(run_id=run_id)
        logger.info("Created UploadPostTool instance (run_id=%s)", run_id)
        return tool_instance

    if tool_name == "analyze_media":
        tool_instance = AnalyzeMediaTool(run_id=run_id)
        logger.info("Created AnalyzeMediaTool instance (run_id=%s)", run_id)
        return tool_instance

    # Unknown tool - pass through as string (might be registered elsewhere)
    logger.warning("Unknown tool '%s', passing through as string", tool_name)
    return tool_name
