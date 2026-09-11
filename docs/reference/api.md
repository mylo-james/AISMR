# API Reference

> **Reference**: Technical specification of REST API endpoints. For a tutorial on using the API, see [Quickstart](../tutorials/quickstart.md).

REST API endpoints.

---

## Authentication

All endpoints (except `/health` and webhooks) require an API key:

```
X-API-Key: your-api-key
```

---

## Endpoints

### Health

```
GET /health
```

Returns service status. No authentication required.

**Response**:
```json
{
  "status": "healthy",
  "version": "0.1.0"
}
```

---

### Start Workflow

```
POST /v1/runs/start
```

Start a new video production workflow.

**Request**:
```json
{
  "project": "aismr",
  "brief": "Create a relaxing rain video"
}
```

**Response**:
```json
{
  "run_id": "uuid",
  "status": "pending"
}
```

---

### Get Run Status

```
GET /v1/runs/{run_id}
```

**Response**:
```json
{
  "run_id": "uuid",
  "status": "awaiting_ideation_approval",
  "current_step": "ideator",
  "artifacts": {}
}
```

**Status values**:
- `pending`
- `running`
- `awaiting_ideation_approval`
- `awaiting_video_generation`
- `awaiting_render`
- `awaiting_publish_approval`
- `completed`
- `failed`

---

### Approve Gate

```
POST /v1/runs/{run_id}/approve
```

Approve a human-in-the-loop gate.

**Request**:
```json
{
  "gate": "ideation",
  "approved": true,
  "comment": "Looks good"
}
```

---

### Chat with Supervisor

```
POST /v1/chat/supervisor
```

Interactive chat with the supervisor agent.

**Request**:
```json
{
  "message": "What videos should I make today?",
  "session_id": "optional-session-id"
}
```

---

## Webhooks

### Sora Standard Webhook

```
POST /v1/webhooks/sora
```

Called by OpenAI Standard Webhooks when video generation completes or fails.
We resolve `run_id` via the stored CLIP_MANIFEST mapping (task_id -> run_id);
`run_id` query param is supported only for legacy/manual callbacks.

Expected event envelope (minimum fields we rely on):

```json
{
  "object": "event",
  "type": "video.completed",
  "data": {
    "id": "video_..."
  }
}
```

Supported event types: `video.completed`, `video.failed`.

On `video.completed`, AISMR downloads the MP4 via `GET /v1/videos/{id}/content`
using the OpenAI API key, then transcodes and resumes the workflow.

Captured contract examples (dashboard test webhooks):

```json
{
  "id": "evt_69449cfb31088190a29b3f98a8450ccb",
  "object": "event",
  "created_at": 1766104315,
  "type": "video.completed",
  "data": {
    "id": "video_abc123"
  }
}
```

```json
{
  "id": "evt_69449cf62cdc8190a854f95b368a5d4d",
  "object": "event",
  "created_at": 1766104310,
  "type": "video.failed",
  "data": {
    "id": "video_abc123"
  }
}
```

### Remotion Callback

```
POST /v1/webhooks/remotion?run_id={run_id}
```

Called by Remotion service when render completes.

---

## Rate Limits

- 60 requests/minute per API key
- 429 response includes `retry_after_seconds`

---

## OpenAPI Spec

Full specification available at `/openapi.json` or in `openapi.json` in the repository.
