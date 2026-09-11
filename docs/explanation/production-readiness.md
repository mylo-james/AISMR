# Production Readiness (Judgement, Not Just Features)

This repo intentionally includes **more “production primitives” than an MVP needs** (LangGraph durability, job queue
leases/DLQ, safety gates, telemetry, eval scaffolding). The workflows are long-running and webhook-driven, so these
primitives protect correctness and make failures debuggable.

This doc makes the tradeoffs explicit:
- what ships in a v0.1 MVP,
- what stays because it protects correctness/safety,
- what scales next and why.

## MVP path (single process, minimal infra)

**Goal:** “One machine can complete a run end-to-end” with safe defaults and low operational overhead.

Recommended knobs:
- `WORKFLOW_DISPATCHER=inprocess` (API runs workflow work in-process)
- `DISABLE_BACKGROUND_WORKFLOWS=false` (workflow actually progresses)
- Fake providers by default while iterating (`*_PROVIDER=fake`)

Keep even in an MVP:
- **Fail-closed safety** (safety errors block requests)
- **Webhook signature verification** (don’t accept unsigned callbacks)
- **Idempotency and DB locking** for webhooks/races
- **Structured logs + request IDs** (debugging is part of the product)

Defer in an MVP (if time/budget constrained):
- Separate worker deployment (run everything in the API process first)
- Advanced operator tooling (DLQ replay CLI, run forking workflows)
- Automated eval gates in CI (keep eval scripts, run manually until iteration stabilizes)

## Scale path (API replicas + worker replicas)

Once runs are valuable enough (cost + reliability pressure), switch to a topology where the API is stateless and
workers execute workflow segments durably.

Recommended knobs:
- `WORKFLOW_DISPATCHER=db` (API enqueues durable jobs to Postgres)
- Run `aismr worker run` in one or more worker processes
- Use shared artifact storage (`TRANSCODE_STORAGE_BACKEND=s3`) for multi-machine scale

Why this is the “right next step”:
- Webhooks stay fast: validate + enqueue in the API; heavy work happens in workers
- Crash recovery becomes boring: leases + retries + LangGraph checkpoints
- Scaling is linear: add API/worker replicas without rewriting orchestration code

More detail: `docs/explanation/scaling.md`

## “What to cut” (showing judgment)

If this looks overbuilt for v0.1, that’s intentional in a few places. If building for a small team under tight timelines,
remove or postpone in this order:

1. **Optional complexity, not correctness**
   - Defer memory bank features until there’s a clear retention/UX requirement.
   - Defer multi-provider surface area until there is real vendor risk (keep boundaries, not integrations).

2. **Operator-only recovery paths**
   - Keep “resume” (it’s directly tied to correctness), but postpone “fork/time-travel” UX until it’s needed.

3. **Non-blocking quality gates**
   - Keep eval tooling, but don’t block CI on it until the product has stable prompts and a clear metric target.

Do not cut:
- Safety posture (fail-closed)
- Webhook verification
- Idempotency / concurrency correctness
- Durable workflow state
