"""AISMR agents module - Llama Stack persona agents.

In 0.3.x, the Agent class has changed:
- NO built-in input_shields/output_shields (use safety module)
- NO sampling_params (configure at server level)
- Uses Conversations API internally

Safety should be handled separately:
    from myloware.safety import check_agent_input, check_agent_output
"""

from myloware.agents.factory import (
    create_agent,
    create_file_search_tool_config,
    create_persona_agent,
    create_rag_tool_config,
)
from myloware.agents.supervisor import create_supervisor_agent

__all__ = [
    # Config-driven agent creation (recommended)
    "create_agent",
    # Low-level factory functions
    "create_persona_agent",
    "create_rag_tool_config",
    "create_file_search_tool_config",
    # Supervisor (has custom tools)
    "create_supervisor_agent",
]
