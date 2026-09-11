# Scaling AISMR (Production Topology)

This document describes a **scale-ready** deployment topology for AISMR that:
- supports **N API replicas** and **M worker replicas**,
- keeps correctness without shared process memory,
- uses **Postgres as the only orchestration dependency** (already required for LangGraph persistence in real mode).

**Important**: video/media artifacts are large binaries. True horizontal scaling across machines requires
**shared artifact storage** (typically S3/R2/MinIO). Local filesystem storage only works when the API and workers
share the same volume.

## Goals

- **Stateless API**: web tier can scale horizontally; any request can hit any replica.
- **Durable orchestration**: workflow progress is recoverable after worker crashes/restarts.
- **Idempotent event handling**: webhooks and client retries are safe under duplication and reordering.
- **Fast webhooks**: the API validates + enqueues; workers do the heavy work.

## Target topology

```
          ┌─────────────────────────┐
          │         Clients         │
          └────────────┬────────────┘
                       │ HTTP
                ┌──────▼──────┐
                │  API (N×)   │   FastAPI + auth + safety + validation
                └──────┬──────┘
                       │ enqueue / persist events
        ┌──────────────▼──────────────┐
        │           Postgres           │
        │  runs + artifacts + jobs +   │
        │  langgraph checkpoints       │
        └──────────────┬──────────────┘
                       │ claim jobs
                ┌──────▼──────┐
                │ Workers (M×)│   run/resume LangGraph, transcode, publish
                └─────────────┘
```

### What runs where

- **API**
  - Auth (`X-API-Key`)
  - Safety middleware (fail-closed)
  - Webhook signature verification + lightweight schema validation
  - Writes durable state: run rows, artifacts, and **jobs**
  - Enqueues work (never relies on in-memory BackgroundTasks for correctness)

- **Workers**
  - Claim jobs from Postgres (`FOR UPDATE SKIP LOCKED`)
  - Execute *short* workflow segments (start to first interrupt, resume from interrupts)
  - Run side effects (transcode, external provider calls) and persist results

## Why Postgres-only

This repo already uses Postgres as the production database and uses Postgres checkpointers for LangGraph durability. Using Postgres as the job queue keeps the architecture simple while still supporting:
- horizontal scaling (add API/worker replicas),
- at-least-once job execution with leases and retries,
- dedupe via idempotency keys.

If the system outgrows Postgres-based job dispatching, the job interface can be swapped for a dedicated queue (Redis/RabbitMQ), without changing workflow semantics.

## Artifact storage (media)

Jobs and state scale cleanly with Postgres, but **media files do not**:
- `TRANSCODE_STORAGE_BACKEND=local` stores transcoded clips under `TRANSCODE_OUTPUT_DIR` and serves them from `/v1/media/transcoded/*`.
  - This requires that **API replicas and workers share the same filesystem** (single host + shared volume).
- `TRANSCODE_STORAGE_BACKEND=s3` uploads transcoded clips to S3-compatible storage and stores `s3://...` URIs in the DB.
  - Before submitting to Remotion, the workflow resolves `s3://...` URIs to **presigned HTTPS URLs** for the renderer.
  - This is the recommended path for **multi-machine** scaling.

## Operational knobs (recommended defaults)

- `WORKFLOW_DISPATCHER=db` (API enqueues jobs; workers execute)
- `DISABLE_BACKGROUND_WORKFLOWS=false` (enable real execution)
- Run **multiple API replicas** (e.g., `uvicorn --workers 2` per instance, and/or multiple instances)
- Run **multiple worker replicas** (scale throughput linearly)

## Failure modes & guarantees

- **At-least-once execution**: jobs may retry after crashes/timeouts.
- **Idempotency**: webhooks and resumes use idempotency keys to prevent duplicate work.
- **Crash recovery**:
  - If a worker crashes mid-job, the job lease expires and another worker retries.
  - LangGraph checkpoints provide state persistence between attempts.

## Non-goals (for this repo)

- Exactly-once semantics across external providers (not possible without provider support).
- Replacing Postgres with a dedicated queue (out of scope for this topology).
