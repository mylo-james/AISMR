# Threat Model (Focused + Practical)

This is a pragmatic threat model for AISMR. It’s not exhaustive; it focuses on the highest-risk surfaces for a
workflow system that executes external provider work via webhooks and processes untrusted URLs.

## Assets

- **API authentication**: `API_KEY` for user-facing endpoints.
- **Webhook secrets**: provider signing secrets (Sora/Remotion/UploadPost).
- **Run state + artifacts**: DB rows, workflow checkpoints, media URIs.
- **Compute environment**: worker machines and their network access.

## Primary attack surfaces

### 1) Webhook endpoints (for provider callbacks)

Threats:
- Forged callbacks that mark runs complete / inject malicious URLs
- Replay attacks (duplicate delivery)
- Timing attacks against signature validation

Controls in this repo:
- HMAC signature verification + constant-time compare
- Fail-closed behavior in production when secrets/signatures are missing
- Idempotency + DB locking for duplicate/out-of-order events

Recommended operational hardening:
- Store secrets in a secrets manager (not `.env`)
- Apply per-endpoint rate limiting and IP allowlists (where providers support stable egress)
- Alert on repeated signature failures

### 2) SSRF via media download/transcode

Threats:
- Attacker provides a URL that reaches internal services (metadata endpoints, localhost, RFC1918)
- DNS rebinding to private IPs

Controls in this repo:
- Explicit SSRF guardrails: hostname allowlists + resolved-IP checks
- Default behavior rejects private networks unless explicitly enabled
- Optional bearer token for `/v1/media/*` endpoints (`MEDIA_ACCESS_TOKEN`)

Recommended operational hardening:
- Enforce egress firewall rules at the network level (best control)
- Keep `TRANSCODE_ALLOW_PRIVATE=false` in production
- Log + alert on blocked SSRF attempts (without leaking target URLs in public logs)

### 3) Remotion composition execution / sandboxing

Threats:
- Untrusted code execution if arbitrary composition code is allowed
- Supply chain / dependency execution inside render environment

Controls in this repo:
- Sandbox flags exist and are documented as security boundaries
- Composition-code execution should be opt-in, not default

Recommended operational hardening:
- Run the renderer in an isolated environment (container/VM with restricted filesystem + network)
- Treat “allow composition code” as production change requiring review

### 4) Prompt / tool injection and unsafe outputs

Threats:
- Model output tries to call tools with unsafe arguments (URLs, file paths, prompt injection)
- Model output contains content that violates policy

Controls in this repo:
- Safety middleware is **fail-closed** (timeouts/errors block requests)
- Shield failures are treated as unsafe in real mode
- HITL gates block publish-critical steps

Recommended operational hardening:
- Continue to enforce strict tool schemas and validate tool args server-side
- Restrict tool capabilities by environment (fake providers in dev/test)

## Assumptions (explicit)

- API is deployed behind HTTPS (terminating TLS at the edge).
- Real-provider mode runs with secrets correctly configured.
- Worker machines are treated as sensitive infrastructure (limited access, monitored).

## Next hardening checklist

- Set composition-code execution to **disabled by default** in `.env.example`.
- Add a fast static security scan in CI (Bandit) and keep it non-flaky.
- Add a lightweight security ADR for webhook + SSRF design decisions.
