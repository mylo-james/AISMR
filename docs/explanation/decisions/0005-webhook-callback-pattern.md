# ADR-0005: Webhook Callback Pattern

**Status**: Accepted
**Date**: 2024-12-06

## Context

Video generation (OpenAI Sora) and rendering (Remotion) are long-running operations (1-5 minutes). The workflow needs to:
1. Submit jobs to external services
2. Continue when jobs complete
3. Handle failures gracefully

Options: polling, webhooks, or WebSockets.

## Decision

**Webhook callbacks.** External services POST to our API when jobs complete.
For OpenAI Sora this uses **Standard Webhooks** configured in the dashboard (no per-request callback_url).

Flow:
```
[AISMR] → POST job to Sora (no callback_url)
                    ↓
[Sora] processes video (1-3 min)
                    ↓
[OpenAI] → POST Standard Webhook (video.completed/video.failed)
                    ↓
[AISMR] → Updates run, continues workflow
```

Webhook URL format (Sora, Standard Webhooks):
```
{WEBHOOK_BASE_URL}/v1/webhooks/sora
```

We resolve `run_id` using the stored CLIP_MANIFEST mapping (task_id → run_id).
`run_id` query params are only used for legacy/manual callbacks.

Remotion uses a per-request callback URL that includes `run_id`:
```
{WEBHOOK_BASE_URL}/v1/webhooks/remotion?run_id={run_id}
```

## Consequences

### Positive

- No polling (resource efficient)
- Immediate continuation when job completes
- Standard pattern (Stripe, GitHub use webhooks)
- Simple HTTP POST, easy to debug

### Negative

- Endpoints must be publicly accessible
- Need to validate webhook authenticity
- Handle duplicates and out-of-order delivery
- Local dev requires tunneling (ngrok)

### Neutral

- Well-understood by developers

## Security

1. **Run ID validation** — Verify run exists and is in expected state
2. **HMAC signatures** — Validate webhook authenticity (Standard Webhooks)
3. **Rate limiting** — Prevent webhook flooding
4. **Idempotency** — Handle duplicate calls gracefully

## Alternatives Rejected

| Option | Why Not |
|--------|---------|
| **Polling** | Wasteful. Slower (polling interval). Complex error handling. |
| **WebSockets** | Overkill for infrequent updates. External services don't support it. |
| **Message queue** | Additional infrastructure. External services don't publish to queues. |

## Implementation

```python
@router.post("/webhooks/sora")
async def sora_callback():
    payload = await request.json()
    task_id = payload["data"]["id"]
    run_id = resolve_run_id_from_manifest(task_id)
    run = run_repo.get(UUID(run_id))
    if run.status != RunStatus.AWAITING_VIDEO_GENERATION:
        raise HTTPException(400, "Invalid state")

    payload = await request.json()
    artifact_repo.create(run_id=run.id, content=payload["video_url"])
    await continue_workflow(run_id)
```

## References

- [Webhook Best Practices](https://webhooks.fyi/)
