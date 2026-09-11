# AISMR local walkthrough

AISMR ends with a playable video and a final visitor review. The current visitor
flow does not publish to TikTok. Existing posting adapters and historical private
posting evidence are separate from this walkthrough. The seven-node workflow view is
a coarse, source-derived projection of persisted run state. It reports observed work
and checkpoints; it is not a live provider trace or an independent readiness claim.

## Modes

| Mode | Inputs and narration | Effects |
|------|----------------------|---------|
| `fixture` | Synthetic video clips and macOS speech | Offline checks and a real local render |
| `recorded` | Hash-verified approved Teacup archive, including the original ElevenLabs batch | Reuses existing media and makes a local edit; no new inference |
| `live` | Twelve Wan video requests and one ElevenLabs narration batch | Requires explicit enablement, credentials, positive budgets and visitor plan approval |

Mac speech is a fixture dependency. It lets developers exercise batching,
silence splitting and editing without paying for voice generation. It is not the
voice used by recorded or live AISMR results. Fixture timestamp positions are
synthetic test inputs, not speech recognition evidence.

## Prerequisites

Use the project's Python 3.13 development environment and installed FFmpeg,
ffprobe and Node dependencies. Fixture mode additionally needs macOS `say`.
Recorded mode requires the approved archive with its fixture manifest and exact
source files. The application verifies archive paths, byte counts and hashes.

Build the renderer from `services/remotion` with `npm run build` after installing
its declared dependencies. From the application checkout, start either:

```bash
.venv/bin/python scripts/aismr_local.py --mode fixture
```

or, using the approved archive's actual absolute path:

```bash
.venv/bin/python scripts/aismr_local.py --mode recorded --recorded-root /absolute/path/to/approved-teacup-v3
```

Open `http://127.0.0.1:8311`. In recorded mode, Teacup is locked and plan revision
is disabled because the media corresponds to that exact approved plan. Approve
the twelve-month plan, follow observed work, play the verified edit and finish
review. No publication decision is created.

The launcher isolates fixture, recorded and live SQLite databases and media
roots below `.local/aismr`. It scrubs inherited provider credential aliases for
fixture/recorded operation. It does not rewrite `.env`.

## Local concurrency and authority

| Process | Address | Limit |
|---------|---------|-------|
| Application API | `127.0.0.1:8311` | Same-origin visitor API |
| Remotion | `127.0.0.1:8312` | One render job and one frame worker |
| Application worker | Durable SQL queue | Two workers |

These limits apply to different queues. Encoding threads and OS scheduling are
separate. Media requests use exact trusted origins, bounded downloads and
signature checks. Visitor decisions require the initiating session, same origin,
CSRF token and current plan/final hash. A shared result link grants read access.

The production join needs twelve verified videos and twelve derived voice clips.
The narration provider receives one batch request. Its timestamp text is checked
against the approved narration, measured silences define split boundaries, and
all twelve WAV results are verified before rendering. The final video can exceed
one source clip's size limit but must fit the configured overall run byte limit,
including retained source assets. The downloaded final is moved into place to
avoid an extra application copy.

The local fixture uses macOS `say` only. It produces synthetic narration for offline
batching, silence-splitting, and renderer checks. A live or recorded result uses the
one voice batch associated with its media source. Live generation performs twelve video
requests and one voice-batch request. `Random` resolves to a server-selected catalog
item before any safety decision, while free text is normalized and then reviewed; neither
choice bypasses moderation or the plan approval gate.

## Recent creations and storage

For a private live evaluation, set `preserve_run_artifacts: true` in the native
launcher's nonsecret runtime JSON and choose a durable `--data-root`. This opt-in
is valid only for live mode on loopback or explicitly enabled private Tailscale
Serve, with public runtime disabled.
It retains all source media, split audio, final files, original narration
timestamps and stored planner request/response payloads. Review expiry,
moderation, visitor approval and spending checks still apply. The gallery still
shows at most three accepted finals, but this private instance does not delete
older files automatically. Retained files consume disk until the owner removes
them explicitly or disables preservation.

The renderer also retains twelve assembled month/title WAVs and a hash manifest
under `output/narration-archive/<job-id>/` in that data root. It still removes
temporary frames and staging files. The native launcher sets a private
`render-tmp` directory on the output filesystem and checks for 4 GiB free before
starting services; the renderer's existing storage guard continues during work.

The initial page shows up to three newest, accepted, hash-verified completed
videos. Fewer completed videos produce fewer entries; there are no filler slots.
Recorded results are labeled. Synthetic fixtures are excluded from the gallery.
Pending final reviews and unapproved or corrupt files are never public examples.

Accepting a final queues cleanup through the existing SQL worker. Cleanup keeps
up to three final files in that mode's media root, removes known generated working
files and fixture inputs, and evicts the oldest final when a fourth completes.
It preserves compact database decisions, hashes and events. Old result pages
explain when a preview has expired. The approved external archive is not part of
cleanup.

Cleanup records a durable intent before deleting files and can finish that
intent after interruption. Unknown files, symlinks, active work, pending reviews
and ambiguous submissions are protected. Corrupt files are excluded rather than
automatically deleted. Renderer staging and composition bundles are removed by
the renderer; its duplicate final is deleted through an authenticated operation
bound to the exact run, render input and final hash. After a renderer restart,
unknown old jobs use the renderer's existing output-age fallback, which runs on
subsequent render submission. Logs and compact database history are not rotated
by the three-video rule.

The gallery streams an open, verified file descriptor through Starlette's native
byte-range handling, so a simultaneous eviction does not redirect playback to a
different path. This mechanism supports the local macOS and hosted Linux runtime
families; it has not established a Windows deployment path.

## Public-library boundary

The normal local gallery is mode-local and private to the local workflow. It is not a
public-library authority. A final can enter the separate public library only after the
visitor explicitly opts in at final review, using the exact reviewed final hash and the
current visibility text. Generic acceptance, fixture output, historical approval, or a
shared preview link does not create public consent.

Before a live or recorded final is projected, the source record must show a verified
final hash, successful final-media moderation, and a passing owner-supplied public-rights
profile. That profile is local evidence, not a license lookup. It must be unexpired and
match the actual twelve ready video assets, one ready narration batch, configured music
identifier and hash, and label-permission evidence. Missing, malformed, expired, or
mismatched evidence leaves the result unverified and private.

Public projections copy verified bytes into `AISMR_LIBRARY_MEDIA_ROOT` and store their
sanitized entries in `AISMR_LIBRARY_DATABASE_URL`. Both locations must be separate from
the source-mode databases and from fixture, recorded, and source-media roots. The public
library keeps at most three active owned finals. Its serialized projection can retire the
oldest entry after a newer accepted projection succeeds; source cleanup occurs only after
that verified projection. Private source runs and previews still expire under their own
review and retention rules. This design has local coverage only and does not establish a
hosted public service, provider rights, owner costs, or deployment readiness.

## Stopping and recovery

Press Ctrl-C in the launcher terminal, or send SIGTERM to the launcher through
its service manager. Both request the same cleanup during startup and normal
operation: terminate its recorded children, wait for exit, and kill an owned child
that exceeds the eight-second grace period. Repeated signals do not interrupt
cleanup. This covers direct children; renderer-owned subprocess cleanup remains
the renderer's responsibility.
Logs are `.local/aismr/api.log`, `worker.log` and `renderer.log`. The PID file can
be stale, so verify process identity before using it.

A worker restart never authorizes fresh paid requests for `submission_unknown`.
Known accepted render receipts may be polled again. A saved `wait_render` error
can retry through LangGraph's native checkpoint retry without submitting another
render. Other checkpoint mismatches remain fail-closed.

## Verification limits

Recorded mode proves archive reuse, orchestration, editing, review, playback and
local storage behavior. It does not prove a new live ideation/generation run or
real moderation quality. The interface reports observed events and exact loaded
or submitted knowledge receipts when present. Application episodic memory is
explicitly unavailable; coding-agent memory is separate.

Live launch consumes the existing dedicated 1Password FIFO for generation and
moderation keys and requires explicit positive run/daily budgets. Local config
validation is not permission for paid execution or proof of public deployment.
TikTok credentials and posting are not prerequisites for the video walkthrough.
Public-library settings and a passing local rights profile add evidence checks, but do
not establish a public release. Owner consent is a final-review action for one exact
verified result, and it does not authorize TikTok posting, a provider call, deployment,
or publication outside the library.

## Isolated studio instances

The native launcher accepts `--api-port`, `--renderer-port`, `--data-root` and
`--origin`. With no overrides it retains the loopback 8311/8312 defaults. Each
selected data root owns its databases, media, renderer output/state, logs and PID
file. The API and renderer bind only to loopback; an HTTPS origin does not create
or configure a reverse proxy. Occupied ports fail before child processes start.

Live mode also accepts `--runtime-config /absolute/path/runtime.json`. This JSON
contains only nonsecret runtime settings: `ideation_backend`,
`planner_version`, `creative_repair_attempts`, `plan_revisions`,
`preserve_run_artifacts`, `private_tailnet_enabled`,
`public_runtime_enabled`, `cookie_secure`, `session_cookie_suffix`,
`library_database_url`, `library_media_root`, `rights_profile_path`,
`cost_profile_version`, the seven `cost_*_usd` settings, and
`active_runs`/`visitor_runs_24h`/`ip_runs_24h`. Unknown keys are rejected. Budgets
remain explicit `--run-budget-usd` and `--daily-budget-usd` arguments. A
nonloopback live origin normally requires the existing public-runtime checks and OpenAI
ideation. A rights-profile path is required; its contents must pass the separate
public-use checks before any result enters the public library.

For an owner-operated live trial over Tailscale Serve, set
`private_tailnet_enabled: true`, `cookie_secure: true` and
`public_runtime_enabled: false`, then pass the exact HTTPS `.ts.net` origin.
This explicit private mode permits the existing Codex planner and retained
artifacts, while requiring the complete cost profile and normal approval gates.
The launcher still binds services only to loopback. Configure the selected port
with native Tailscale Serve and verify that Funnel is disabled for it before
activation. The hostname alone is not proof of private exposure; this setting
must never be used for Funnel or another public proxy.

Vendor credentials are still read only from the native
`.local/aismr/credentials.env` FIFO, regardless of `--data-root`. Generated local
session secrets belong to the selected data root. Give independent studios on
the same host different `session_cookie_suffix` values so opening one does not
replace the other's session cookie. Existing names remain unchanged when this
setting is omitted.

`--check-config` assembles and validates the configuration without starting a
service or requesting generation. In live mode it reads the native credential
FIFO and can create the selected local directory and generated session-secret
file. It is not proof of provider-account health or public-use eligibility.
