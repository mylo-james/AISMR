# Environment Variables

All configuration options for AISMR.

---

## Required

| Variable | Description |
|----------|-------------|
| `API_KEY` | API authentication key |
| `LLAMA_STACK_URL` | Llama Stack server URL |

---

## Llama Stack

| Variable | Default | Description |
|----------|---------|-------------|
| `LLAMA_STACK_URL` | — | Base URL (e.g., `http://localhost:5001`) |
| `LLAMA_STACK_MODEL` | `openai/gpt-5-nano` | Default model |
| `LLAMA_STACK_PROVIDER` | `real` | `real\|fake\|off` (fake avoids network in tests/dev) |
| `OPENAI_API_KEY` | — | OpenAI API key (embeddings + Sora when enabled) |
| `BRAVE_API_KEY` | — | For web search tool |

---

## Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+psycopg2://myloware:myloware@localhost:5432/myloware` | Database connection string |

---

## Providers & Webhooks

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBHOOK_BASE_URL` | — | Public URL for callbacks (required for real providers in prod) |
| `OPENAI_STANDARD_WEBHOOK_SECRET` | — | Standard Webhooks secret (`webhook-signature`) for OpenAI events (e.g., Sora `video.completed`) |
| `OPENAI_SORA_SIGNING_SECRET` | — | Sora HMAC secret (legacy/fallback) |
| `SORA_PROVIDER` | `real` | `real\|fake\|off` |
| `SORA_FAKE_CLIPS_DIR` | `fake_clips/sora` | MP4 fixtures directory (fake mode) |
| `SORA_FAKE_CLIP_PATHS` | — | Comma-separated MP4 paths (fake mode) |
| `REMOTION_SERVICE_URL` | — | Remotion render service |
| `REMOTION_API_SECRET` | — | Remotion authentication |
| `REMOTION_WEBHOOK_SECRET` | — | HMAC secret for verifying Remotion callbacks (API side) |
| `WEBHOOK_SECRET` | — | Remotion service webhook signing secret (service side; should match `REMOTION_WEBHOOK_SECRET`) |
| `REMOTION_PROVIDER` | `real` | `real\|fake\|off` |
| `UPLOAD_POST_API_KEY` | — | Upload-Post API key (required when provider is real) |
| `UPLOAD_POST_API_URL` | `https://api.upload-post.com` | Upload-Post API base URL |
| `UPLOAD_POST_PROVIDER` | `real` | `real\|fake\|off` |
| `UPLOAD_POST_POLL_INTERVAL_S` | `10.0` | Polling interval (seconds) when Upload-Post returns async request_id |
| `UPLOAD_POST_POLL_TIMEOUT_S` | `600.0` | Polling timeout (seconds) for async Upload-Post publishes |
| `MEDIA_ACCESS_TOKEN` | — | Optional bearer token required for `/v1/media/*` endpoints |
| `PUBLIC_DEMO_ENABLED` | `false` | Enable public demo endpoints (motivational-only) |
| `PUBLIC_DEMO_ALLOWED_WORKFLOWS` | `motivational` | Comma-separated allowlist for public demo workflows |
| `PUBLIC_DEMO_TOKEN_TTL_HOURS` | `72` | TTL for public demo run tokens |
| `PUBLIC_DEMO_RATE_LIMIT` | `10/minute` | Rate limit for demo run starts |
| `PUBLIC_DEMO_CORS_ORIGINS` | `https://aismr.mjames.dev` | Comma-separated CORS allowlist for demo UI |

---

## Scaling / Workers

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKFLOW_DISPATCHER` | `inprocess` | `inprocess\|db` (`db` enqueues durable jobs to Postgres for worker processes) |
| `WORKER_CONCURRENCY` | `4` | Max concurrent jobs per worker process |
| `WORKER_ID` | — | Optional worker identifier (auto-generated if empty) |
| `JOB_POLL_INTERVAL_SECONDS` | `1.0` | Worker poll interval when no jobs are available |
| `JOB_LEASE_SECONDS` | `600.0` | Job lease duration; workers renew while running |
| `JOB_MAX_ATTEMPTS` | `5` | Default retry attempts for queued jobs |
| `JOB_RETRY_DELAY_SECONDS` | `5.0` | Base retry delay (worker applies simple backoff) |

---

## Transcode Storage (Media)

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCODE_STORAGE_BACKEND` | `local` | `local\|s3` (`s3` recommended for multi-replica) |
| `TRANSCODE_OUTPUT_DIR` | `/tmp/myloware_videos` | Local output dir for transcoded clips (must be shared between API and workers) |
| `TRANSCODE_ALLOW_FILE_URLS` | `false` | Allow `file://` URLs for transcode inputs (local-only) |
| `TRANSCODE_S3_BUCKET` | — | S3 bucket when `TRANSCODE_STORAGE_BACKEND=s3` |
| `TRANSCODE_S3_PREFIX` | `myloware/transcoded` | Object key prefix for uploaded clips |
| `TRANSCODE_S3_ENDPOINT_URL` | — | Optional endpoint for S3-compatible storage (R2/MinIO) |
| `TRANSCODE_S3_REGION` | — | Region for AWS S3 client (if required) |
| `TRANSCODE_S3_PRESIGN_SECONDS` | `86400` | Presigned GET TTL for renderer access |

Note: S3 mode requires `boto3` (install with `pip install 'aismr[s3]'`) and standard AWS credentials
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN`).

---

## Safety

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_SAFETY_SHIELDS` | `true` | Enable Llama Guard |
| `CONTENT_SAFETY_SHIELD_ID` | `together/meta-llama/Llama-Guard-4-12B` | Shield identifier / model ID |

**Note**: Safety is fail-closed. Shield errors block requests.
In production code, shields are forced on (setting `ENABLE_SAFETY_SHIELDS=false` is ignored).

---

## Remotion Sandbox

| Variable | Default | Description |
|----------|---------|-------------|
| `REMOTION_SANDBOX_ENABLED` | `false` | Enable sandbox mode |
| `REMOTION_ALLOW_COMPOSITION_CODE` | `false` | Allow dynamic code |
| `REMOTION_SANDBOX_STRICT` | `false` | Require strict sandbox enforcement before enabling dynamic code |

**Warning**: Only enable `ALLOW_COMPOSITION_CODE` in isolated environments.

---

## Remotion Service (services/remotion)

| Variable | Default | Description |
|----------|---------|-------------|
| `REMOTION_MAX_JOBS` | `1000` | Max in-memory job records retained |
| `REMOTION_JOB_TTL_SECONDS` | `21600` | Prune completed/error jobs older than this |
| `REMOTION_OUTPUT_TTL_SECONDS` | `86400` | Prune rendered MP4s older than this |
| `REMOTION_OUTPUT_PUBLIC` | `false` | Serve `/output` without auth when true |
| `REMOTION_CALLBACK_ALLOWLIST` | — | Comma-separated hostname allowlist for render callbacks |
| `REMOTION_CALLBACK_TIMEOUT_MS` | `5000` | Timeout for webhook callbacks (ms) |
| `REMOTION_RENDER_TIMEOUT_SECONDS` | `900` | Max render duration before cancellation |
| `REMOTION_JOB_CONCURRENCY` | `1` | Concurrent render jobs in the service. Legacy `CONCURRENCY` is honored only when this is unset. |
| `REMOTION_FRAME_CONCURRENCY` | `1` | Frame workers inside each render job (positive number or percentage). |
| `REMOTION_MEDIA_ALLOWED_ORIGINS` | — | Required comma-separated exact media origins for monthly renders, such as a trusted fal media origin or a loopback fixture origin. Redirects are rejected. |
| `REMOTION_MAX_MEDIA_BYTES` | `67108864` | Maximum downloaded bytes for one monthly media asset |
| `REMOTION_MAX_JOB_STAGED_BYTES` | `268435456` | Maximum combined staged bytes for one monthly render job |
| `REMOTION_MEDIA_FETCH_TIMEOUT_MS` | `30000` | Source-media download deadline in milliseconds |
| `REMOTION_PROBE_TIMEOUT_MS` | `15000` | Local ffprobe and final decode verification timeout in milliseconds |
| `FFPROBE_PATH` | `ffprobe` | Command or absolute path used for media metadata probing |
| `FFMPEG_PATH` | `ffmpeg` | Command or absolute path used for final media decode verification |
| `REMOTION_BUNDLE_CACHE_MAX` | `32` | Max cached bundles to keep in memory |
| `REMOTION_BUNDLE_CACHE_TTL_SECONDS` | `3600` | Bundle cache TTL (seconds) |
| `REMOTION_BROWSER_IDLE_TTL_SECONDS` | `300` | Close Chromium if idle for this long (seconds) |

`REMOTION_FRAME_CONCURRENCY=1` is valid and passes the numeric value `1` to Remotion. Local defaults are one render job multiplied by one frame worker, so the numeric upper bound is `REMOTION_JOB_CONCURRENCY × REMOTION_FRAME_CONCURRENCY`. Encoding threads are separate. A percentage such as `100%` resolves per host and cannot be multiplied as a fixed number. Production may set either control independently.

---

## AISMR visitor studio

`StudioSettings` reads variables prefixed with `AISMR_`. The local launcher sets
fixture values for its child processes only. It neither rewrites `.env` nor changes
these defaults for ordinary application startup. `AISMR_MEDIA_ALLOWED_ORIGINS` is a
Pydantic JSON list when set directly. The launcher supplies a JSON value so an older
comma-separated `.env` example cannot affect its child processes.

| Variable | Default | Description |
|----------|---------|-------------|
| `AISMR_ENABLED` | `false` | Enable the studio routes and service composition. |
| `AISMR_MODE` | `fixture` | `fixture\|recorded\|planning\|local\|live`. The standard launcher defaults to fixture mode. Recorded mode requires `AISMR_RECORDED_ROOT`. Private planning mode uses real Codex agents and text moderation, ends at idea review, and requires `creative-v2` with live media disabled. See [creative planning](creative-planning.md). Local mode runs the full private workflow with real agents, saved provider media and a local renderer. |
| `AISMR_LIVE_ENABLED` | `false` | Separate effect gate. Live mode requires `true`. |
| `AISMR_PUBLIC_RUNTIME_ENABLED` | `false` | Enable the additional public-admission and library prerequisites for a live runtime. It does not make any run public by itself. |
| `AISMR_PRIVATE_TAILNET_ENABLED` | `false` | Explicit private Tailscale Serve live trial. Requires an HTTPS `.ts.net` origin, secure cookies, public runtime disabled and a complete cost profile. Allows Codex and private artifact retention. Never use with Funnel or a public proxy. |
| `AISMR_ADMISSION_PAUSED` | `false` | Static startup pause for new live admissions. A durable owner pause may also block admission. |
| `AISMR_FIXTURE_MODERATION` | `allow` | Fixture outcome: `allow\|deny\|malformed\|outage`. |
| `AISMR_ORIGIN` | `http://127.0.0.1:8311` | One HTTP(S) origin. Non-loopback origins require HTTPS and secure cookies. |
| `AISMR_COOKIE_SECURE` | `false` | Require secure session cookies. |
| `AISMR_SESSION_COOKIE_SUFFIX` | unset | Optional lowercase suffix (1–32 letters, digits or underscores; starts with a letter) for an independent studio session cookie on the same host. Existing cookie names stay unchanged when unset. |
| `AISMR_SESSION_HOURS` | `24` | Visitor session lifetime, from 1 to 168 hours. |
| `AISMR_VISITOR_RUNS_24H` | `1` | Visitor run quota. |
| `AISMR_IP_RUNS_24H` | `4` | IP run quota. |
| `AISMR_ACTIVE_RUNS` | `2` | Maximum active visitor runs. |
| `AISMR_FIXTURE_UNLIMITED_ADMISSIONS` | `false` | Development fixture override for visitor, network and open-run admission caps. Rejected in live or recorded mode. Worker/render concurrency, pause controls and session authority remain enforced. Does not reset storage by itself. |
| `AISMR_DAILY_BUDGET_USD` | `0` | Daily budget. A positive value is required by live validation. |
| `AISMR_RUN_RESERVATION_USD` | `0` | Per-run reservation. A positive value is required by live validation. |
| `AISMR_COST_PROFILE_VERSION` | unset | Owner-assigned version for the complete public-run cost profile. |
| `AISMR_COST_INPUT_MODERATION_USD` through `AISMR_COST_FINAL_MODERATION_USD` | unset | Seven owner-supplied nonnegative amounts: input moderation, ideation, plan moderation, one video request, narration batch, render, and final moderation. Live admission needs every amount to be positive. |
| `AISMR_ASSET_RETRIES` | `1` | Provider retry limit, from 0 to 2; live retries require an exact reserved attempt. Local receipt retrieval is separately capped at three attempts. |
| `AISMR_PLAN_REVISIONS` | `1` | Visitor plan revision limit, from 0 to 2; pinned at admission for new planner runs. |
| `AISMR_PLANNER_VERSION` | `single-v1` for live; `creative-v2` for unspecified fixture settings | Version for new plans. V2 reserves four to six calls per planning version and supports targeted revisions. See [creative planning](creative-planning.md). |
| `AISMR_IDEATION_DEADLINE_SECONDS` | `120` | Per-call planning deadline, from 1 to 120 seconds. The Codex subprocess has a stricter 90-second cap. Pinned on creative-plan admission. |
| `AISMR_CREATIVE_REPAIR_ATTEMPTS` | `0` | Extra validation-repair text calls per new creative planning revision, from 0 to 2. Pinned at admission with separate receipts and live reserves; does not change existing runs or extend deadlines. |
| `AISMR_CREATIVE_WORKFLOW_DEADLINE_SECONDS` | `660` | Whole creative workflow window, from 1 to 660 seconds, pinned separately from the per-call deadline. Existing planner rows without this field retain their stored historical deadline. Does not add calls, retries or token allowance. |
| `AISMR_POLL_SECONDS` | `3` | Studio polling interval. |
| `AISMR_JOB_CONCURRENCY` | `2` | Studio job concurrency setting. Worker process concurrency uses `WORKER_CONCURRENCY`. |
| `AISMR_RUN_DEADLINE_HOURS` | `24` | Run deadline, from 1 to 48 hours. |
| `AISMR_RETENTION_DAYS` | `7` | Retention setting. It does not install or run a cleanup process. |
| `AISMR_MEDIA_ROOT` | `.local/aismr/media` | Local verified render-output root. |
| `AISMR_FIXTURE_ROOT` | `.local/aismr/fixtures` | Synthetic fixture video and narration root. |
| `AISMR_RECORDED_ROOT` | unset | Absolute or relative root of the approved recorded archive. Required only in recorded mode. |
| `AISMR_LOCAL_MEDIA_ROOT` | unset | Verified saved-media archive for local mode. Its manifest identity is pinned at admission. See [local media workflow](local-media-workflow.md). |
| `AISMR_LIBRARY_DATABASE_URL` | unset | Separate SQLite or PostgreSQL database for public-gallery entries. It must differ from the source-mode database. |
| `AISMR_LIBRARY_MEDIA_ROOT` | unset | Separate public-library media root. It must not overlap fixture, recorded, or source media. |
| `AISMR_RIGHTS_PROFILE_PATH` | unset | Path to the owner-supplied, unexpired public-rights receipt profile. |
| `AISMR_MEDIA_ALLOWED_ORIGINS` | Fal media origins | Configured source asset origins. Fixture, recorded and local media paths can explicitly admit the app origin; live provider fetches do not admit loopback. |
| `AISMR_MAX_ASSET_BYTES` | `67108864` | Per-asset download cap. |
| `AISMR_MAX_RUN_BYTES` | `268435456` | Aggregate run download cap. |
| `AISMR_RENDER_REAL` | `false` | Require a real Remotion renderer. Live and local validation require `true`. |
| `AISMR_MUSIC_ID` | `tender-moment` | Expected background-music identifier. A public rights receipt must also match its exact file hash and license evidence. |

Local live validation requires `AISMR_LIVE_ENABLED=true`, a Fal key, an OpenAI key,
a unique session secret of at least 32 characters, positive run and daily budgets, and
`AISMR_RENDER_REAL=true`. It is intentionally narrower than provider readiness or public
release approval. The local launcher reads only `FAL_API_KEY` and `OPENAI_API_KEY`
from `.local/aismr/credentials.env`, which must be the native 1Password
FIFO mount. It keeps live SQLite and media roots separate from fixture state, passes no
vendor credentials to Remotion, disables legacy Llama, Sora, and upload-post providers,
and still uses OpenAI for monthly moderation. `--check-config` only validates this
configuration and starts no services or external requests. It reads that FIFO and
may create the selected data directory and local session-secret file.

When `AISMR_PUBLIC_RUNTIME_ENABLED=true`, readiness adds a separate library database and
media root, an unpaused admission state, and a complete versioned cost profile. The
whole-run reservation includes input moderation, ideation, plan moderation, twelve video
requests, one narration batch, rendering, final moderation, and the configured plan
revisions. The application does not choose provider prices: the owner must supply and
review the positive amounts. A budget holds unresolved or unknown charges conservatively.
Public runtime also cannot use Codex ideation because that backend is loopback-only.
TikTok credentials and posting are outside this visitor walkthrough and are not public
runtime prerequisites.

See [`aismr-local.md`](aismr-local.md) for fixture operation and the live boundary.

---

## Development

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_FAKE_PROVIDERS` | `false` | Convenience switch: treat providers as fake in dev/tests |
| `DISABLE_BACKGROUND_WORKFLOWS` | `false` | Skip background workflow execution (fast tests) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
