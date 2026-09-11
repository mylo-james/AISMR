# AISMR deployment

The public target is `https://aismr.mjames.dev`. The selected recorded walkthrough
uses Vercel, Neon Postgres and Cloudflare R2. It needs no Fly service or separate
renderer. A deployment receipt, rather than this source document, establishes
whether the public hostname is live.

## Runtime

- Vercel serves the existing visitor UI and the narrow FastAPI entry point in
  `app.py`. It does not start the legacy application, provider probes or a worker loop.
- Vercel Queues invokes `queue_worker.py`. Each invocation claims at most one
  existing SQL job, advances up to four immediate recorded LangGraph steps within
  a 65-second limit, and releases its claim. It stops at each human review.
  Review decisions remain bound to the visitor, plan revision and content hash.
  Expired recorded walkthroughs release admission capacity while preserving
  their review history; expired runs cannot accept decisions.
- Neon stores visitors, jobs, decisions, assets and native LangGraph checkpoints.
  Use a separate AISMR database and a restricted runtime role. Initialize all
  Alembic revisions and checkpoint tables with the migration role first.
- Private R2 stores the canonical sample media. Runtime access is read-only.
  Object keys include their content hash. Per-run cleanup never deletes these
  shared objects. Authorized playback receives a five-minute signed URL after
  checking the verified final's current ETag.

The recorded flow loads a pinned Teacup plan, waits for plan approval, binds its
24 scene/narration references to the archived manifest, selects the prepared
final, streams that final from R2 to verify its SHA-256 and size, then waits for
final acceptance. No current ideation, moderation, media generation, rendering,
gallery publication or social posting is claimed. The direct gallery route is
closed in this mode. Only the exact recorded plan can reuse this final.

## Configuration and release

1. Install the locked project with the `hosted` extra. Vercel installs the matching
   default `deployment` dependency group through its native Python installer,
   which can optimize the function bundle. Keep the Vercel project
   root at the repository root. `vercel.json` and `queue_worker.py` are native
   Vercel configuration and subscriber-discovery inputs.
2. Configure the names in `deploy/vercel.env.example` through encrypted Vercel
   environment variables. Generate independent session and API secrets.
   Keep all generation and posting keys empty. Recorded final reuse rejects
   live/render flags and those provider credentials.
3. Initialize only the selected AISMR Neon database with `alembic upgrade head`
   and native checkpoint setup. Use verified TLS with the runtime's CA bundle.
   The migration environment accommodates the existing long revision IDs without
   changing their history. The runtime role needs application/checkpoint data
   access, not schema creation or checkpoint migration writes.
4. Upload the approved media bundle using an AISMR-bucket-scoped temporary
   credential. Verify each local hash before upload and each R2 copy after upload.
   Never upload original private provider receipts or source paths. Never overwrite
   an existing content-addressed object unless its existing bytes verify first.
5. Stage a reviewed source with `vercel deploy --prod --skip-domain`. Verify
   `/health`, the native subscriber, both visitor review gates, cold resume,
   unauthorized access and actual final playback before promoting the staged ID.
6. Assign `aismr.mjames.dev` to the AISMR Vercel project. Use the CNAME Vercel
   reports for this domain in Cloudflare DNS, with proxying disabled as in the
   sibling projects. Verify the canonical HTTPS origin and secure session cookie.

`.github/workflows/deploy.yml` offers manual stage/promote operations, disabled
until project variables are configured. It deploys from the root. Git pushes do
not automatically deploy. Preserve the source manifest, configuration revision,
staged deployment ID and verification receipt outside the application checkout.

The [earlier live preparation](aismr-live-deployment-preparation.md) remains
reference material. Enabling new generation later requires its actual provider,
moderation, budget and rendering integration checks. Adding an UploadPost key
does not connect the current visitor flow to posting.
