# Earlier live-generation deployment preparation

This document retains the earlier persistent-renderer proposal. It is not the selected public deployment. Use [the current recorded deployment](aismr-deployment.md) for Vercel, Neon and R2.

# AISMR deployment preparation

The current visitor flow ends at a verified video and final review. UploadPost
is not connected to this flow. Adding its key does not enable posting. The target
is **https://aismr.mjames.dev**. Launch also needs a selected backend host,
durable storage, HTTPS, a public-use rights
profile and a verified OpenAI planning run.

Use [the paused application template](../../deploy/aismr-app.env.example) to
prepare the host configuration. It has the canonical domain, no credentials, zero
budgets, live effects disabled and admissions paused.
It does not install services or select a paid hosting plan.

## Runtime layout

Match the existing portfolio, Isntgram and Mad Moon map convention: Vercel for the
frontend and Cloudflare-managed canonical DNS. Isntgram also establishes Neon
Postgres and Cloudflare R2 as the shared database/storage direction. Provision
separate AISMR resources in those accounts; do not reuse another app's database,
secrets, bucket or deployment project.

Deploy `web/demo` as the Vercel project root, using its `vercel.json`. The sole
backend rewrite sends `/v1/studio/:path*` to the proposed backend hostname
`api.aismr.mjames.dev`, keeping browser cookies, requests and signed renderer
callbacks on the canonical studio origin. API responses are not CDN-cacheable.
The frame policy allows embedding in the portfolio. Native Git auto-deployment
is disabled in this configuration; stage and promote a reviewed source explicitly.

The Python API, SQL worker and Remotion run together on a persistent container
host using [compose.aismr.yml](../../compose.aismr.yml). These processes use local
media and a long-lived render queue, which do not fit an ordinary Vercel Function's
execution and scratch-filesystem lifecycle. Vercel supports external rewrites for
this split. [Vercel runtimes](https://vercel.com/docs/functions/runtimes),
[external rewrites](https://vercel.com/docs/routing/rewrites).

API and worker share source and library media mounts at identical absolute paths.
The source and library databases are separate Neon databases, with a direct
connection for the checkpointer and migrations. The template uses `postgresql`
for source and `postgresql+asyncpg` for the library, with `PGSSLMODE=verify-full`
and `PGSSLROOTCERT=/etc/ssl/certs/ca-certificates.crt` in each process
environment. The API image installs that CA bundle. Installed asyncpg reads that native setting and
builds a certificate-validating, hostname-checking TLS context. Libpq/Psycopg
uses the same setting. Use URLs without `sslmode` or `channel_binding` query
parameters: those libpq parameters are not the asyncpg URL keyword contract.
Verify the actual Neon endpoint, certificate rejection and both DB paths at
activation; local parsing is not a live connection test.

R2 is not yet a replacement for AISMR's source or gallery filesystem. The legacy
transcode S3 setting does not make those paths object-storage-backed. Keep shared
persistent media volumes for this prepared configuration; an R2 media adapter and
its retention/streaming tests remain separate work if full storage alignment is
required before launch.

The renderer output volume preserves bytes, not its in-memory job registry or
verification receipt. Pause admissions and wait for all accepted renders to
finish before a planned restart. If the renderer crashes mid-render, preserve the
original records and reconcile manually. A file on disk does not authorize a
replacement receipt or an automatic repeat. No full in-flight restart recovery is
claimed.

| Configuration | Required setup |
| --- | --- |
| API / worker secrets | Unique `API_KEY`, `AISMR_SESSION_SECRET` (at least 32 characters), `REMOTION_API_SECRET`, and `REMOTION_WEBHOOK_SECRET`. Store outside the image and repository. |
| OpenAI | `OPENAI_API_KEY` or `AISMR_OPENAI_API_KEY`; public planning selects `AISMR_IDEATION_BACKEND=openai`. Source default model is `gpt-4o-mini`; account access, output quality and full creative-v2 execution still need a live check. Moderation uses the same credential. |
| Fal | `FAL_API_KEY` or `AISMR_FAL_KEY`; video generation and ElevenLabs narration use Fal. Preserve the approved execution profile and saved month-bank assets. |
| HTTPS | Set the exact `AISMR_ORIGIN` and matching `WEBHOOK_BASE_URL`, secure cookies, and proxy trust restricted to the actual ingress. A public hostname cannot use the private Codex/Tailscale exception. |
| Renderer network | `REMOTION_SERVICE_URL` in API/worker must resolve to the private renderer. Renderer `PUBLIC_BASE_URL` must be reachable by the API/worker; it need not be public internet access. |
| Renderer secrets | Set `REMOTION_API_SECRET` to the matching application secret and `WEBHOOK_SECRET` to the application's `REMOTION_WEBHOOK_SECRET`. Give no vendor API keys to the renderer. |
| Renderer boundaries | `NODE_ENV=production`, `REMOTION_OUTPUT_PUBLIC=false`, `REMOTION_ALLOW_COMPOSITION_CODE=false`, one `REMOTION_JOB_CONCURRENCY` and one `REMOTION_FRAME_CONCURRENCY`. |
| Renderer requests | `REMOTION_MEDIA_ALLOWED_ORIGINS` is the exact studio origin; `REMOTION_CALLBACK_ALLOWLIST` is its HTTPS hostname. Verify that signed asset retrieval and callbacks work through the selected ingress. |
| Public gallery | Supply a truthful, unexpired profile matching actual video, narration, saved month-bank, music and label evidence. See the [rights-profile example](../reference/aismr-public-rights-profile.example.json). A path alone is not a passing receipt. |
| Budgets | Replace zeroes with a reviewed versioned profile and positive overall limits. The reservation includes all creative calls, repair/revision allowances, twelve clips and one narration batch. Reusing the private Codex cost estimate does not price the OpenAI deployment. |

The template disables legacy Llama Stack, Sora, public-demo and UploadPost paths.
The studio uses its own selected OpenAI/Fal composition and existing moderation
checks. It does not need the legacy Llama Stack ingest service for this flow.
An unavailable legacy KB status must not be mistaken for a failed studio render;
verify the actual studio configuration and workflow separately.

## UploadPost integration still required

`src/myloware/tools/publish.py` is the older agent tool. Historical studio
publication adapters use their own approval receipts. The current visitor store
does not issue posting authority. Do not wire the old tool directly to final
acceptance or reuse gallery consent as permission to post.

The next integration must bind an explicit posting decision to the exact final
file hash, destination/profile, caption and visibility, then persist the provider
request identifier before submission. Keep ambiguous results held for status
reconciliation and avoid duplicate submissions on timeout. Test disconnected
accounts, unsupported privacy, rejected uploads, lost responses and inbox-only
results before attempting a real post.

UploadPost documents a separate profile with a connected TikTok account. Check
its capabilities and the account's allowed privacy controls before upload. Its
guide currently says TikTok requires a paid plan; confirm account eligibility
before subscribing. [TikTok setup](https://docs.upload-post.com/guides/post-to-tiktok-api/),
[profile capabilities](https://docs.upload-post.com/api/user-profiles/).

The upload API accepts a file or URL, while asynchronous status uses a request
identifier. Completion can represent delivery to the TikTok inbox rather than
a live post. The integration must report that distinction and require a verified
post URL before claiming publication. [Upload API](https://docs.upload-post.com/api/upload-video/),
[status and inbox behavior](https://docs.upload-post.com/api/upload-status/).

## Launch sequence

1. Select the backend host and exact source candidate for `aismr.mjames.dev`. The
   development checkout contains uncommitted application work; the historical
   repository HEAD alone does not identify the current application.
2. Build both images on the target Linux architecture. Verify `/`, `/app.js`,
   `/progress.js`, `/styles.css`, the current role knowledge, music and saved
   month-bank assets inside the images. Run the focused local gate before using
   hosted validation for the complete candidate.
3. Inject local service secrets and available provider keys privately. Set up
   durable storage and back it up before migration. Apply source migrations once,
   start with live effects disabled and both static and persisted admission pause
   enabled. The durable controls are `aismr studio pause-admissions` and
   `aismr studio admission-status`, each with the selected `--database-url`.
4. Verify HTTPS, cookies, gallery fallback, signed media/callback rejection,
   internal renderer isolation, persistence after restart and admission refusal
   from another machine. Empty gallery and unavailable generation are expected
   until their prerequisites are satisfied.
5. After the host, model, rights evidence and spending allowance are settled,
   authorize one bounded provider smoke and then one full visitor run. Changing
   the static pause or live flags requires a process configuration restart; the
   CLI alone cannot clear a static pause. Keep the normal idea and final-review
   decisions. Restore pause and reconcile costs after the run.
6. Exercise posting separately after its integration and exact publication
   authorization. A watchable video, gallery acceptance and upload acceptance
   each establish different outcomes.

For rollback, pause admissions first, retain databases and accepted/unknown
provider receipts, then stop only the deployment's own services. Do not roll a
database back across successful provider work or automatically regenerate stages.
Restore from a verified backup only with the corresponding source and migration
identity.

## Current verification limits

Local process tests cover SIGINT and SIGTERM during startup and normal operation,
including child termination and reaping. Configuration tests cover public OpenAI
selection and fail-closed prerequisites. These are credential-free checks.

The preparation environment has no Docker executable, so image build, Linux
execution, selected-host restart/persistence, live OpenAI planning and UploadPost
publication remain unverified. The local renderer separately requires at least
4 GiB free at startup; inspect current free space before restoring a private trial.

## Host activation inputs

Copy [compose.env.example](../../deploy/compose.env.example) outside the repository
and fill the exact image digests, absolute private environment-file paths, storage
root, rights-file path and trusted ingress addresses. The app image runs as UID
1000; prepare its source/library directories with that ownership before startup.
Mounts must already exist as real directories/files, with enough render headroom.
The renderer has its own [environment template](../../deploy/aismr-renderer.env.example).

Use a separately scoped migration credential in the migration environment file,
with `PGSSLMODE=verify-full`, the CA bundle and selected source `DATABASE_URL`.
Use the [migration template](../../deploy/aismr-migration.env.example). The application
currently performs table-existence/bootstrap checks and LangGraph checkpointer
setup on startup. Validate its runtime role against those actual operations; do
not claim Isntgram's exact restricted-role setup transfers unchanged. Set up the
separate library tables with its approved database role as well.

On the approved host, the native sequence is:

```bash
docker compose --env-file /absolute/private/compose.env -f compose.aismr.yml config --quiet
docker compose --env-file /absolute/private/compose.env -f compose.aismr.yml --profile migration run --rm migrate
docker compose --env-file /absolute/private/compose.env -f compose.aismr.yml up -d api renderer worker
```

Configuration validation does not deploy. The next two commands mutate the
selected database/runtime and are only for approved host activation. The migration
must succeed before starting API/worker; the profile deliberately keeps migration
out of routine `up` and service restarts. Back up the selected databases first.

Configure TLS ingress for the backend hostname to the loopback-bound API. Trust
only the real ingress hop and sanitize forwarded-client headers so visitor/IP
quotas cannot be bypassed with a caller-supplied header. Set Cloudflare records to
the targets returned by each host's native domain verification, as for the other
projects; do not copy another project's CNAME or assume a backend IP.

The `myloware` Python namespace and old CLI alias are retained for compatibility.
All new app, image, domain and command naming uses AISMR. Workspace relocation and
GitHub repository rename have not been performed.

## Frontend release workflow

The manual `AISMR frontend deployment` workflow follows the sibling pattern:
serialized production operations, exact source SHA, a staged deployment with no
canonical-domain change, then explicit promotion of its reviewed deployment ID.
Git pushes do not deploy this app. Before enabling it, configure the AISMR Vercel
project in the existing team, select `web/demo` as its root, configure native domain
verification, and fill the production environment's `VERCEL_ORG_ID`,
`AISMR_VERCEL_PROJECT_ID`, `AISMR_CONFIG_REVISION` and private `VERCEL_TOKEN`.
`AISMR_DEPLOYMENT_ENABLED` and `AISMR_PROMOTION_ENABLED` are separate disabled-by-default
controls. Retain the project's production environment approval protection.

Stage only a reviewed candidate whose applicable CI/security gates passed. Review
the staged source/configuration metadata and its protected preview, then promote
that exact deployment ID. Save the previous deployment ID for rollback. The
workflow deploys only the frontend; backend activation, Neon migration and DNS
changes are separate operations. The workflow has been prepared locally and has
not been dispatched or verified against Vercel in this phase.
