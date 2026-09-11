# AISMR

AISMR is a LangGraph application for creating a reviewed twelve-scene surreal
video. Enter an object, review and revise the ideas, approve production, follow
video and narration generation, and review the finished edit. Visitor decisions,
moderation, spending reservations and durable workflow records control each stage.

The public destination is **https://aismr.mjames.dev**. Deployment preparation
uses Vercel for the UI, API and queued workflow wake-ups, Neon for durable state,
and Cloudflare DNS/R2 for the hostname and saved media. The recorded walkthrough
reuses an existing plan, assets and final video through both visitor review gates.
It makes no new generation, rendering or posting requests. A hosted verification
receipt is required before describing the public launch as complete.

## Local studio

Use Python 3.13, Node.js, FFmpeg and the project's development dependencies.
Build the renderer after installing its declared dependencies:

```bash
npm ci --prefix services/remotion
npm run build --prefix services/remotion
.venv/bin/python scripts/aismr_local.py --mode fixture
```

Open `http://127.0.0.1:8311`. The default fixture mode uses synthetic media and
macOS speech with a real local render. Recorded mode reuses a verified archive.
Live mode requires working OpenAI/Fal credentials, explicit budgets and normal
visitor approvals. The public runtime uses OpenAI planning; local private trials
can use the existing Codex transport.

See [local setup and recovery](docs/reference/aismr-local.md),
[configuration](docs/reference/env.md), and
[creative planning](docs/reference/creative-planning.md).

## Deployment

Start with [AISMR deployment preparation](docs/how-to/aismr-deployment.md):

- `vercel.json`, `app.py`, `queue_worker.py`: native FastAPI and queue deployment.
- `deploy/vercel.env.example`: recorded-mode configuration with private storage access.
- `data/recorded/teacup`: pinned archive metadata and the saved plan; media stays in R2.

The previous Fly.io/Firebase setup is preserved only as
[historical reference](docs/archive/legacy-deployment/README.md). CI no longer
has an automatic Fly deployment step on a main-branch push.

## Posting status

The current visitor journey ends at final video review. UploadPost integration
and an explicit posting decision are still required before a key can enable
TikTok posting. Gallery acceptance is not posting approval.

## Development

Install the project with development extras to expose the `aismr` command:

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
aismr --help
make lint
make type-check
make test-fast
```

The Python import namespace remains `myloware` as a compatibility boundary for
existing integrations and saved workflow state. The old CLI alias also remains
available for retained private service definitions. The repository URL and local
workspace directory still use their existing identities; changing those requires
a coordinated relocation and remote rename, not a text replacement.

## License

MIT. Shipped media has its own [license information](data/media/LICENSE.md).
