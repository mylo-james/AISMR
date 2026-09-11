# ADR 0010: LangGraph for Workflow Orchestration

**Status**: Accepted
**Date**: 2025-12-12
**Deciders**: AISMR Team
**Tags**: architecture, workflows, orchestration, state-management

## Context

AISMR v1 used LangChain + LangGraph for both agent logic and orchestration. v2 rewrote the AI layer on Llama Stack for direct control, but we still needed robust workflow orchestration capabilities:

- **Stateful workflows**: Multi-step video production with state persistence
- **Human-in-the-loop (HITL)**: Approval gates that pause and resume
- **Crash recovery**: Ability to resume workflows after failures
- **Time-travel debugging**: Inspect workflow state at any point in history
- **Webhook integration**: Pause workflows waiting for external callbacks

## Decision

We will use **LangGraph** for workflow orchestration only, while keeping Llama Stack for all AI operations (agents, inference, RAG, safety). Vector/hybrid search uses **Milvus** when configured (otherwise **pgvector**); Postgres is used for workflow SQL needs (runs, artifacts, checkpoints, DLQ).

### Data stores
- **Postgres**: LangGraph checkpoints (`langgraph-checkpoint-postgres`), runs/artifacts, DLQ, admin state.
- **Milvus**: Dense/sparse/hybrid retrieval for knowledge; no vector storage in Postgres.

### Architecture

```
User/API → LangGraph (Orchestration) → Llama Stack (AI Operations) → External Services
```

**LangGraph provides:**
- State machine workflow definition (`StateGraph`)
- Checkpointing with PostgreSQL (`PostgresSaver`)
- Native `interrupt()` for HITL gates
- `Command(resume=...)` for resuming after interrupts
- Time-travel debugging via checkpoint history

**Llama Stack provides:**
- Direct agent creation and execution
- LLM inference
- RAG operations
- Safety shields
- Telemetry

**Milvus provides:**
- Dense + sparse + hybrid retrieval for knowledge/RAG
- Scalar filters on metadata
- Isolation from workflow state persistence (no vectors in Postgres)

### Implementation Details

1. **State Schema**: `VideoWorkflowState` TypedDict defines all workflow state
2. **Nodes**: 8 workflow nodes (ideation, approval, production, wait_for_videos, editing, wait_for_render, publish_approval, publishing)
3. **Graph**: Compiled with `PostgresSaver` for persistence
4. **API**: `/v2/runs/*` endpoints for LangGraph workflows (feature-flagged)
5. **Webhooks**: Resume workflows with `Command(resume=...)` when external services complete

## Consequences

### Positive

- **Robust orchestration**: Native checkpointing and crash recovery
- **HITL support**: Built-in `interrupt()` primitive for approval gates
- **Debuggability**: Time-travel debugging via checkpoint history
- **Separation of concerns**: LangGraph for orchestration, Llama Stack for AI, Milvus for retrieval
- **Production-ready**: PostgreSQL-backed persistence for reliability
- **Hybrid search retained**: Milvus stays the retrieval back-end; no need to force pgvector

### Negative

- **Additional dependency**: LangGraph and langgraph-checkpoint-postgres
- **Learning curve**: Team needs to understand LangGraph patterns
- **Migration effort**: Existing workflows need to be ported to LangGraph nodes
- **Two datastores**: Milvus for vectors, Postgres for workflow state/checkpoints

### Risks

- **Feature flag complexity**: Need to maintain both old and new workflow paths during migration
- **State migration**: Existing runs won't have LangGraph checkpoints
- **Performance**: Checkpointing adds database overhead (mitigated by PostgreSQL performance)
- **Cross-store consistency**: Need stable IDs to link Milvus docs to Postgres artifacts when required

## Alternatives Considered

1. **Custom state machine**: Build our own orchestration layer
   - Rejected: Too much work, LangGraph already solves this well

2. **Keep existing orchestrator**: Continue with current step-based approach
   - Rejected: No checkpointing, no crash recovery, no time-travel debugging

3. **Use LangGraph for everything**: Bring back LangChain for agents
   - Rejected: We want direct control over AI operations (see ADR 0001)

## References

- [Why Llama Stack](../why-llama-stack.md) - v1 → v2 evolution
- [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)
- [ADR-0001: Llama Stack as Foundation](0001-llama-stack-foundation.md)
