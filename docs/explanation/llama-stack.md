# Llama Stack Integration

How AISMR uses Llama Stack.

---

## Why Llama Stack?

Llama Stack provides a unified API for:
- **Inference** — LLM calls
- **Tools** — Web search, RAG, custom tools
- **Safety** — Llama Guard shields
- **Telemetry** — OpenTelemetry traces

One SDK, one client, no framework-on-framework complexity.

---

## Distribution Configuration

AISMR's Llama Stack config lives in `llama_stack/run.yaml`:

```yaml
inference:
  - provider_id: together
    provider_type: remote::together

models:
  - model_id: meta-llama/Llama-3.2-3B-Instruct
  - model_id: meta-llama/Llama-Guard-3-8B

vector_io:
  - provider_id: milvus
    provider_type: inline::milvus

telemetry:
  - provider_id: meta-reference
    config:
      sinks: [otel_trace]
      otel_exporter_otlp_endpoint: http://jaeger:4318
```

---

## Agent Pattern

Agents use the client-side `Agent` class:

```python
from llama_stack_client.lib.agents.agent import Agent

agent = Agent(
    client,
    model="meta-llama/Llama-3.2-3B-Instruct",
    instructions="You are...",
    tools=[custom_tool],
)

with agent_session(client, agent, session_name) as session_id:
    response = agent.create_turn(
        messages=[{"role": "user", "content": prompt}],
        session_id=session_id,
        stream=False,
    )
```

**Note**: Sessions are cleaned up automatically via `client.conversations.delete()`.

---

## Safety Shields

Safety checks are manual (not built into Agent):

```python
from myloware.safety import check_content_safety, check_agent_output

# Before agent call
result = await check_content_safety(client, user_input)
if not result.safe:
    raise HTTPException(400, f"Blocked: {result.reason}")

# After agent call
result = await check_agent_output(client, response_text)
if not result.safe:
    raise ValueError("Output blocked")
```

---

## Custom Tools

Tools extend `ClientTool` with JSON Schema:

```python
from llama_stack_client.lib.agents.client_tool import ClientTool

class MyTool(ClientTool):
    def get_name(self) -> str:
        return "my_tool"

    def get_description(self) -> str:
        return "Description for the LLM"

    def get_input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "param": {"type": "string"}
            },
            "required": ["param"]
        }

    async def run_impl(self, param: str) -> dict:
        return {"result": "..."}
```

---

## Vector I/O (RAG)

Knowledge base uses Milvus with hybrid search:

```python
# Search modes
results = client.vector_stores.search(
    store_id=store_id,
    query=query,
    search_mode="hybrid",  # vector + BM25
    ranking_options={"ranker": {"type": "rrf"}}
)
```

Documents are ingested on startup from `data/knowledge/`.

---

## Built-in Tools

| Tool | Purpose |
|------|---------|
| `builtin::websearch` | Brave Search |
| `builtin::rag/knowledge_search` | Query vector stores |
| `builtin::memory/query` | Query memory banks |

---

## Telemetry

All agent operations are traced automatically:

```python
# Query traces for a run
traces = client.telemetry.query_traces(
    attribute_filters=[{"key": "run_id", "value": run_id}],
    limit=20,
)
```

View in Jaeger UI at `http://localhost:16686`.

---

## Provider Flexibility

Swap inference providers without code changes:

```yaml
# Together AI (default)
inference:
  - provider_id: together
    provider_type: remote::together

# Ollama (local)
inference:
  - provider_id: ollama
    provider_type: remote::ollama
    config:
      url: http://localhost:11434
```

---

## Further Reading

- [Llama Stack Documentation](https://github.com/meta-llama/llama-stack)
- [Together AI Models](https://together.ai/models)
