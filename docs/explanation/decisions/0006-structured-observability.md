# ADR-0006: Structured Observability

**Status**: Accepted
**Date**: 2024-12-06

## Context

AISMR has multiple observability needs:

1. **LLM tracing** — What did the agent say? How long did inference take?
2. **Request tracing** — Follow a request through the system
3. **Debugging** — Why did this workflow fail?
4. **Cost tracking** — How many tokens did this run use?

Current state: Langfuse for LLM tracing, basic `logging.info()` to stdout.

What's missing: structured logs, request IDs, correlation between logs and traces.

## Decision

**Langfuse for LLM observability + structured JSON logs for everything else.**

### Langfuse (LLM Layer)

Already in place. Provides:
- Token usage per agent call
- Latency breakdowns
- Prompt/completion inspection
- Cost estimation
- Eval scoring

### Structured Logging (Application Layer)

All logs emit JSON with standard fields:

```json
{
  "timestamp": "2024-12-06T10:30:00Z",
  "level": "INFO",
  "message": "Workflow started",
  "request_id": "req-abc123",
  "run_id": "run-def456",
  "user_id": "user-789",
  "step": "ideation",
  "duration_ms": 1234
}
```

Request ID propagation:
```python
# Middleware sets request_id in context
request_id = contextvars.ContextVar("request_id")

# All logs include it automatically
logger.info("Processing", extra={"run_id": run.id})
# Output: {"request_id": "req-abc", "run_id": "def", ...}
```

### What We're NOT Doing (Yet)

- **Prometheus metrics endpoint** — Langfuse covers what we need. Add if/when Langfuse is insufficient.
- **Full OpenTelemetry** — Overkill for personal scale. Llama Stack has OTel integration if needed later.

## Consequences

### Positive

- Searchable logs (JSON works with any log aggregator)
- Request correlation (follow request_id through system)
- Langfuse handles the hard part (LLM tracing, token counting)
- No additional infrastructure

### Negative

- JSON logs are verbose in console (mitigated: pretty-print in dev)
- Must manually add context to log calls
- Two systems to check (Langfuse for LLM, logs for app)

### Neutral

- Fly.io captures stdout as logs automatically
- Can add CloudWatch/Datadog later without code changes

## Alternatives Considered

| Option | Why Not |
|--------|---------|
| **Prometheus + Grafana** | Additional infrastructure. Langfuse sufficient for now. |
| **OpenTelemetry full stack** | Overkill for personal project. Can add later. |
| **Plain text logs** | Not queryable. No correlation. |

## Implementation

```python
# src/myloware/observability/logging.py
import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)

logger = structlog.get_logger()

# Usage
logger.info("workflow_started", run_id=run.id, brief=brief)
```

## References

- [Langfuse Documentation](https://langfuse.com/docs)
- [structlog](https://www.structlog.org/)
- [12 Factor App: Logs](https://12factor.net/logs)
