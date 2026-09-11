"""Supervisor agent for workflow management."""

from __future__ import annotations

from typing import Any, List

from llama_stack_client import LlamaStackClient
from llama_stack_client.lib.agents.agent import Agent

from myloware.agents.factory import create_persona_agent
from myloware.agents.roles import append_role_contract, validate_role_tool_configuration
from myloware.agents.tools.supervisor import (
    ApproveGateTool,
    GetRunStatusTool,
    ListRunsTool,
    StartWorkflowTool,
)
from myloware.config.loaders import load_agent_config
from myloware.observability.logging import get_logger
from myloware.tools.role_knowledge import RoleKnowledgeSearchTool

logger = get_logger(__name__)

__all__ = ["create_supervisor_agent"]


def create_supervisor_agent(
    client: LlamaStackClient,
    model: str | None = None,
    vector_db_id: str = "project_kb",
    project: str = "aismr",
) -> Agent:
    """Create Supervisor agent with workflow management tools.

    Unlike other agents, the supervisor requires runtime-instantiated custom tools
    (StartWorkflowTool, ApproveGateTool, etc.) that can't be defined in YAML.
    Instructions are loaded from YAML config while tools are built dynamically.

    Args:
        client: Llama Stack client
        model: Optional model override
        vector_db_id: Vector DB for RAG
        project: Project name for config loading

    Returns:
        Configured supervisor Agent
    """
    # Load instructions from YAML config
    config = load_agent_config(project, "supervisor")
    instructions = config.get("instructions", "")

    if not instructions:
        raise ValueError("No instructions found in supervisor config")
    validate_role_tool_configuration("supervisor", config.get("tools", []))
    instructions = append_role_contract("supervisor", instructions)

    # Build tools list - custom tools + builtin tools
    tools: List[Any] = [
        # Custom AISMR tools for workflow management
        StartWorkflowTool(vector_db_id=vector_db_id),
        GetRunStatusTool(),
        ListRunsTool(),
        ApproveGateTool(vector_db_id=vector_db_id),
        RoleKnowledgeSearchTool(project=project, role="supervisor"),
    ]

    # Use model from config if not overridden
    model_id = model or config.get("model")

    agent = create_persona_agent(
        client=client,
        persona_name="supervisor",
        instructions=instructions,
        tools=tools,
        model_id=model_id,
    )

    logger.info("Supervisor agent created with %d tools", len(tools))
    return agent
