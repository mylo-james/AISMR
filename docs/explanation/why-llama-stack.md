# Why Llama Stack (v2)

AISMR uses Llama Stack for “AI operations” (inference, tool calling, safety shields, RAG, tracing) and LangGraph for workflow orchestration (state machine, interrupts/HITL, checkpointing).

## Architecture split

```
API / Worker
  └─ LangGraph (workflow state + interrupts)
      └─ Node calls Llama Stack Agent (single turn)
          ├─ Inference (model/provider via config)
          ├─ Tool calls (JSON Schema; executed client-side)
          ├─ Safety shields (fail-closed)
          └─ RAG (file_search / Vector I/O)
```

This keeps the workflow layer explicit and debuggable, while keeping inference/tooling behind a consistent SDK surface.

## Why not an “everything” agent framework

Earlier versions used higher-level agent frameworks for inference + tools. That worked, but debugging wrong behavior required understanding framework-specific abstractions and version drift.

In v2:

- LangGraph is used only for orchestration semantics (state + checkpointing + HITL).
- Llama Stack is used for the LLM-facing surface (inference, tools, safety, RAG).

## Code pointers

- Agent config + construction: `src/myloware/agents/factory.py`
- Workflow graph: `src/myloware/workflows/langgraph/graph.py`
- Workflow nodes (ideate/produce/edit/publish): `src/myloware/workflows/langgraph/nodes.py`
- Llama Stack clients + resilience: `src/myloware/llama_clients.py`
- Safety middleware (fail-closed): `src/myloware/api/middleware/safety.py`
