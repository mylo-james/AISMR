# Safety Model

How AISMR handles content moderation.

---

## Philosophy

**Fail-closed by default.** If safety checks fail or timeout, requests are blocked. Silent failures are worse than loud ones.

---

## Layers

### 1. Input Shields

All write endpoints (`POST /v1/runs/start`, etc.) run content through Llama Guard before processing.

```python
# Safety middleware (automatic)
result = await check_content_safety(client, request_body)
if not result.safe:
    return Response(status_code=400, detail=result.reason)
```

### 2. Agent Output Moderation

After each agent turn, output is checked before proceeding:

```python
# In orchestrator
result = await check_agent_output(client, agent_response)
if not result.safe:
    raise ValueError(f"Agent output blocked: {result.reason}")
```

### 3. Keyword Fallback

If Llama Guard is unavailable, a keyword filter provides basic coverage. This is a fallback, not a replacement.

---

## Configuration

Safety enforcement is designed to be **hard to disable**. Shields are fail-closed and forced on by code.

| Setting | Value | Changeable? |
|---------|-------|-------------|
| Shields enabled | `true` | No (code change required) |
| Fail-closed | `true` | No (code change required) |
| Shield ID / model | `together/meta-llama/Llama-Guard-4-12B` | Yes (`CONTENT_SAFETY_SHIELD_ID`) |

---

## Shield Responses

### Safe Content

Request proceeds normally.

### Unsafe Content

```json
{
  "detail": "Content blocked by safety shield",
  "reason": "violence"
}
```

HTTP 400 response.

### Shield Error/Timeout

```json
{
  "detail": "Safety check failed",
  "reason": "timeout_blocked"
}
```

HTTP 400 response. Request is blocked, not allowed through.

---

## What's Checked

| Content | Check Point |
|---------|-------------|
| User brief | Input middleware |
| Ideator output | After agent turn |
| Producer prompts | After agent turn |
| Editor output | After agent turn |
| Publisher caption | After agent turn |

---

## Limitations

Safety shields are not perfect:

- **False positives**: Legitimate content may be blocked
- **False negatives**: Some harmful content may pass
- **Language coverage**: Best for English

For production, consider additional moderation:
- Human review queues
- Platform-specific content policies
- Media-specific scanning (images, video)

---

## Observability

Safety decisions are logged:

```json
{
  "event": "safety_check",
  "safe": false,
  "reason": "violence",
  "request_id": "abc-123"
}
```

Traces include safety spans with timing.

---

## Testing

```bash
# Test safety middleware
pytest tests/unit/test_safety_middleware.py -v

# Test output moderation
pytest tests/unit/test_output_moderation.py -v
```

In fake mode (`LLAMA_STACK_PROVIDER=fake` or `USE_FAKE_PROVIDERS=true`), shield calls are skipped and the keyword-only filter is used.
