# Architecture

> **Explanation**: This document explains *how* AISMR is designed and *why* those decisions were made. For the evolution from v1 to v2, see [Why Llama Stack](why-llama-stack.md).

How AISMR is designed and why.

---

## Overview

AISMR is a multi-agent video production platform. Users submit a brief, agents collaborate to produce a video, and the result is published to TikTok.

```
User Brief → Supervisor → Ideator → Producer → Editor → Publisher → TikTok
                              ↓          ↓          ↓
                           [HITL]    [Webhook]  [Webhook]
```

---

## Design Principles

### Llama Stack Native

AISMR uses [Llama Stack](https://github.com/meta-llama/llama-stack) as its only AI framework. No LangChain, no additional abstractions.

**Why**: Single API surface for inference, tools, RAG, safety, and telemetry. Swap providers (Together AI → Ollama) without code changes.

### Config-Driven Agents

Agent behavior is defined in YAML, not code. New projects require new config files, not new code.

**Why**: Separation of concerns. Domain experts can tune prompts without touching Python.

### Human-in-the-Loop Gates

Workflows pause at defined gates for human approval before proceeding.

**Why**: AI should augment human judgment, not replace it. Publishing requires explicit approval.

### Fail-Closed Safety

Safety shields are always on. Errors block requests rather than allowing unsafe content through.

**Why**: Content moderation failures should be obvious, not silent.

---

## Components

### API Layer

FastAPI application with:
- Rate limiting (SlowAPI)
- Request ID correlation
- API key authentication
- Safety middleware on write endpoints

### Workflow Engine

Orchestrates multi-agent workflows:
1. **Supervisor** — Routes requests, manages state
2. **Ideator** — Generates video concepts
3. **Producer** — Creates video prompts for OpenAI Sora
4. **Editor** — Composes video with Remotion
5. **Publisher** — Posts to TikTok

#### LangGraph Orchestration (v2)

- Engine: LangGraph `StateGraph` with Postgres checkpoints (resumable, crash-safe).
- State: `VideoWorkflowState` TypedDict (run_id, brief, artifacts, approvals, status, errors).
- Nodes: ideation → ideation_approval → production → wait_for_videos → editing → wait_for_render → publish_approval → publishing; conditional edges for HITL rejections.
- Interrupts: `langgraph.types.interrupt` used for HITL gates and webhook waits.
- Persistence: `langgraph-checkpoint-postgres` uses the primary Postgres DB; thread_id = run_id.
- Entry points: `/v2/runs/*` REST routes start/resume/reject and expose state/history.
- Webhooks: Sora and Remotion webhooks resume the graph with `Command(resume=...)`.

### External Services

| Service | Purpose |
|---------|---------|
| Llama Stack | AI inference, tools, RAG, safety |
| OpenAI Sora | Video generation |
| Remotion | Video composition/rendering |
| PostgreSQL | Run state, artifacts |
| Jaeger | Distributed tracing |

---

## Data Flow

```
1. POST /v1/runs/start
   ↓
2. Safety shield checks brief
   ↓
3. Ideator generates ideas (uses RAG + web search)
   ↓
4. HITL gate: await approval
   ↓
5. Producer creates video prompts
   ↓
6. OpenAI Sora generates clips (webhook on complete)
   ↓
7. Editor composes final video
   ↓
8. Remotion renders (webhook on complete)
   ↓
9. HITL gate: await publish approval
   ↓
10. Publisher posts to TikTok
```

---

## Database Schema

### Runs

Tracks workflow execution state.

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `status` | String | Current state |
| `current_step` | String | Active agent |
| `input` | Text | Original brief |
| `artifacts` | JSON | Produced outputs |
| `error` | Text | Failure reason |

### Artifacts

Stores outputs from each agent.

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `run_id` | UUID | Parent run |
| `persona` | String | Agent role |
| `artifact_type` | String | Output type |
| `content` | Text | Output data |

---

## Scaling Considerations

### Current Scale

- Single API instance
- SQLite or single PostgreSQL
- ~$20/month on Fly.io

### When to Scale

| Trigger | Action |
|---------|--------|
| DB ops >30% latency | Add asyncpg |
| Multiple users | Add JWT auth |
| Render queue >5 | Add Lambda provider |
| Costs >$100/mo | Consider AWS |

### What's Already Scalable

- Stateless API (database-backed sessions)
- Webhook-driven async (no long-running connections)
- Config-driven agents (no code changes for new projects)

---

## Security Model

| Layer | Implementation |
|-------|---------------|
| Authentication | API key header |
| Rate Limiting | Per-key, per-IP |
| Input Validation | Pydantic schemas |
| Content Safety | Llama Guard shields |
| Webhook Auth | HMAC-SHA256 signatures |

---

## Observability

All requests are traced with OpenTelemetry:

- Request ID propagation
- Agent turn spans
- Tool execution timing
- Safety check outcomes

View traces at Jaeger UI (port 16686).
