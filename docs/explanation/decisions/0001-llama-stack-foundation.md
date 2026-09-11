# ADR-0001: Llama Stack as Foundation

**Status**: Accepted
**Date**: 2024-12-06

## Context

AISMR is an AI video production platform that needs:

- Multi-agent orchestration with tool calling
- RAG for domain knowledge (video editing, social media trends)
- Safety guardrails for content moderation
- Observability for debugging agent behavior
- Flexibility to swap inference providers (Together AI → Ollama → local)

The AI framework landscape is crowded: LangChain, LlamaIndex, CrewAI, AutoGen, and Meta's Llama Stack. Each adds abstraction, but abstraction has costs.

## Decision

**Llama Stack is the sole AI framework** for agents, inference, RAG, safety, and telemetry. We avoid LangChain-style agent frameworks. LangGraph is used strictly for workflow orchestration (see ADR-0010).

Why Llama Stack:

1. **Single API surface** — Inference, tools, RAG, safety, and telemetry under one SDK
2. **Provider flexibility** — Swap Together AI for Ollama without code changes
3. **No framework-on-framework** — Direct SDK calls, no abstraction stack to debug
4. **Meta backing** — Active development, native Llama model support

Integration points:
- `llama_stack_client.lib.agents.agent.Agent` — All agents
- `llama_stack_client.lib.agents.custom_tool.CustomTool` — External tools
- `client.vector_stores` — RAG operations
- OpenTelemetry integration — Tracing

## Consequences

### Positive

- Simpler debugging (no framework layers)
- Faster iteration (direct SDK access)
- Future-proof for Llama models

### Negative

- Smaller ecosystem than LangChain
- Less community content
- SDK is sync-first (see ADR-0006)

### Neutral

- Learning curve comparable to other frameworks
- Tools are portable if we ever migrate

## Alternatives Rejected

| Option | Why Not |
|--------|---------|
| **LangChain** | Framework-on-framework anti-pattern. Debugging nightmare. |
| **CrewAI** | Builds on LangChain. Too opinionated for our workflow. |
| **Direct API calls** | Reinventing agent loops, tool execution, RAG. No benefit. |

## References

- [Llama Stack Documentation](https://llama-stack.readthedocs.io/)
- [Why Not LangChain](https://news.ycombinator.com/item?id=37568975)
