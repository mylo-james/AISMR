# Creative planning

`creative-v2` runs a native LangGraph planning subgraph inside the existing `ideate` stage. Two explorers generate short concept cards in parallel. A curator selects and orders twelve concepts, and a shot writer expands them into video prompts. The existing input/plan safety checks and exact-plan human approval still precede media generation.

## Selection and compatibility

`AISMR_MODE=planning` runs the real `creative-v2` text agents and input/plan moderation for private browser feedback. It requires the Codex backend, OpenAI moderation credentials, a private session secret, and `AISMR_LIVE_ENABLED=false`. The API may use loopback or a secure Tailnet origin; the worker that invokes Codex must use loopback. Ordinary live-mode restrictions remain unchanged.

Planning review offers targeted revision and **Keep ideas**. Keeping ideas records approval and ends the run as `plan_complete`; it cannot start video, narration, rendering, final review, or gallery publication. Only actively ideating plans consume planning worker slots. Visitor/IP admission limits, revision limits, operation receipts, moderation and planning deadlines remain enforced. A zero media reservation in this mode does not represent a measurement of native text-account usage or charges.

Use a separate database and session-cookie suffix for this mode. Existing fixture runs stay fixtures and cannot be upgraded in place. Preserve actual planning history across restarts so subsequent feedback can use it; fixture reset routines must not run against the planning database.

`AISMR_MODE=local` continues through all seven workflow nodes with real creative agents and moderation, using saved media in place of third-party generation. It requires `AISMR_LOCAL_MEDIA_ROOT`, the private Codex configuration above, `AISMR_RENDER_REAL=true`, and both live effects and public runtime disabled. Fal and publishing credentials are excluded. The configured archive supplies twelve unchanged videos and title-only narration; the local renderer adds the saved month audio and labels. See [local media workflow](local-media-workflow.md).

A local runtime can continue an existing planning run under its original planning-only behavior. It never upgrades that run to media production. Other runtime/run mode mismatches remain rejected.

`AISMR_PLANNER_VERSION=creative-v2` selects the scene planner for newly admitted runs. Live settings default to `single-v1`; selecting a planner does not enable live effects. Fixture mode uses `creative-v2` revision handling when the version is unspecified, but its plans and media remain deterministic samples. Recorded mode retains its existing v1 path.

Admission records version, actual model/backend, prompt/resource contract hash, per-call and workflow deadlines, deadline-policy fingerprint, and revision allowance in `studio_planner_runs`. Runs without that row use the original single-call path. A configuration change never moves an existing run to another planner. A changed model/backend, contract, or deadline-policy fingerprint blocks further v2 planning rather than silently changing a paid run's behavior. Existing `studio:<run-id>` checkpoint identities and seven outer graph node names remain unchanged.

Migration `010_studio_creative_planning` adds the planning tables, and `011_studio_execution_profile` pins scene execution profiles for new runs. Neither migration backfills or resets old runs. Apply migrations through the existing database migration procedure before running the updated service against an existing database. Local fixture startup uses the existing metadata creation path. This change does not apply a migration to an operational database.

## Bounded roles and receipts

| Role | Job | Maximum input bytes | Maximum output tokens (OpenAI) |
| --- | --- | ---: | ---: |
| `explore_object` | Fifteen compact object-grounded concepts | 24,000 | 3,000 |
| `explore_surreal` | Fifteen compact concepts with impossible physical actions | 24,000 | 3,000 |
| `curate` | Rank concepts for the requested scene positions | 64,000 | 1,800 |
| `replenish` | One additional batch if selection is incomplete | 64,000 | 3,000 |
| `recurate` | Fill the missing positions after that one batch | 64,000 | 1,800 |
| `write_shots` | Expand selected concepts while retaining their IDs, material and action | 32,000 | 4,800 |

The normal path makes four text calls. One replenishment and recuration cycle permits six total calls per planning version. The configured single user revision permits two versions, so admission reserves twelve possible planning calls. Input and plan moderation are each reserved once per version. Each provider operation has a persisted deadline of at most 120 seconds, and Codex has its stricter 90-second subprocess deadline. The full persisted workflow window is 660 seconds: five serial provider stages on the replenishment path plus 60 seconds for the two durable moderation calls. Restarting cannot reset either deadline. The selected token caps are bounds, not a measured guarantee that every provider will finish within them.

OpenAI SDK retries are disabled so retries cannot hide extra model requests. The Codex transport uses its existing isolated, tool-free subprocess, byte bounds and process deadline; its output-token argument is not a hard CLI token limit. The application additionally bounds each recorded response in bytes. Model prices and actual charges still require provider billing evidence.

Each operation has a unique run/revision/role identity. SQL commits the intent before the request and saves the result after it returns. Completed results can be reused without calling the provider again. An intent without a result is uncertain and stops automatic submission. Claim fencing still governs each write. The parent graph can natively retry a failed v2 ideation task using these receipts; it does not restart the entire media flow.

Records contain bounded task inputs, selected-card reasons and outputs. They do not store model hidden reasoning. Curator judgments apply to candidate cards; deterministic writer linkage does not prove final prompt quality or video feasibility. The first release leaves final-plan creative assessment to human review and offline comparison.

## Targeted validation repair

`AISMR_CREATIVE_REPAIR_ATTEMPTS=2` opts newly admitted creative plans into at most two extra text calls per planning revision. The default is `0`, preserving existing deployment inventories. Admission pins the allowance and repair prompt/schema in the run contract. Old profiles without this field retain the original zero-repair contract. Changing the active setting never grants extra calls to an existing run.

A definitely received invalid response can be returned to its source role with bounded record, field and rule diagnostics. Repairs patch only listed invalid fields; valid records, selected concept facts, retained scenes and completed upstream steps remain intact. A wholly unreadable role response may be replaced because it contains no trusted records. Parallel explorers consume repair slots in fixed object-then-surreal order, so saved requests replay deterministically.

Each correction has its own `repair_1` or `repair_2` receipt. The request binds the original received response and earlier correction receipts by hash. Completed corrections replay without provider calls. Uncertain submissions, transport failures, cancellation, expired deadlines and moderation failures do not start a repair. An invalid result after the configured attempts stops with `creative_repair_exhausted`.

Repair calls retain the source role's byte/token limits and the existing workflow deadline. Live admission reserves both slots for every allowed revision before starting; existing budget ceilings are unchanged. With two repairs enabled, the worst case is eight text calls per version. Local-media mode still uses saved assets, and text usage remains separate from media-generation costs. The timeline reports a correction as received until the plan actually passes validation and moderation.

## Storyboard handoff

Planner profile `creative-v2` uses prompt contract `creative-planning-v9` and private response schema version 5. It produces the immutable `ScenePlan` schema version 2, with exactly twelve ordered scenes. Explorers start with an object-specific visible event, then choose materials that make that event readable. The curator compares the event and its payoff across the set: changing the material while repeating the same coating, filling or duplication action does not make a different concept. Concept actions, hooks and payoffs allow up to 120 characters so compact complete phrases fit. Explorers should target complete actions of roughly 8-14 words rather than truncating a sentence to fit the cap.

The curator returns ranked candidate choices without scene ordinals. The backend deterministically assigns them to requested scene positions in rank order before the writer runs. The shot writer returns `ordinal`, `candidate_id`, five storyboard directions (`opening_frame`, `action`, `payoff_frame`, `camera`, and `light_and_texture`), plus private `title_modifier` and `title_noun` fields. The backend binds the selected object, material, and compact action before compilation, so the writer cannot paraphrase or replace those facts. Empty directions or the former standalone `visual_prompt` response are invalid. `storyboard.action` is a complete expanded source-shot direction. The writer chooses distinct modifiers before storyboards; the backend rejects title collisions with both selected and retained scenes. Object and anatomy guidance remain a model instruction, so human review still assesses whether the resulting scene depicts the selected object's actual parts. The service compiles these fields into `SceneIdea.visual_prompt`, within its 1,200-character limit, and composes the public title from one evocative modifier plus the object's concise noun or natural compound noun. The compiled prompt and title are included in the human-reviewed v2 plan hash and reach the video provider without another writing pass.

These directions describe one continuous vertical source shot of approximately 6.7 seconds. The live provider requests 161 frames at 24 fps; the production edit preset uses 202 frames at 30 fps. New scene fixture videos are 6.734 seconds; legacy v1 fixture clips remain 3.375 seconds. No cuts or audio instructions belong in the video prompt; narration and editing remain separate responsibilities.

The role count, per-call deadline, Codex subprocess deadline, token caps, and admission cost profile are unchanged. The whole-workflow deadline is now separately bounded at 660 seconds so the permitted five-stage critical path can finish without extending any provider call or adding a retry. The prompt/schema change creates a different planning contract hash, so existing pinned planning operations cannot silently consume the new contract. Complete fields and exact candidate linkage are structural checks. Actual model outputs still need review for complete action syntax, object fit, repetition, timing and a visible payoff.

## Revisions and recent context

An idea-revision decision can include:

```json
{
  "revision_feedback": {
    "replace_ordinals": [3, 7, 12],
    "reason": "repetitive",
    "note": "Use more distinct visible actions."
  }
}
```

The ordinary gate, revision, exact subject hash, idempotency key, visitor session, origin and CSRF requirements still apply. Omitted ordinals mean all twelve. Ordinals must be unique integers from 1 through 12. Reasons are `repetitive`, `not_object_specific`, `unclear_action` or `other`; notes are optional and limited to 500 characters. Feedback is accepted only for an idea revision. Repeating a decision key with different feedback is a conflict.

A partial revision keeps every unselected title and visual prompt exactly. Narration is derived after plan approval from the title. It creates a new full plan and hash for human approval. Targeted revision is unavailable after production begins. Legacy plans retain their original full-plan revision action.

The context loader uses at most twelve recent selected concepts for the same object and visitor. Explicit approval and selected-for-revision outcomes are distinguished; cancellation is not a dislike score. Feedback remains data in the model request. It cannot create global exclusions. Context is snapshotted and hash-bound before calls so retries do not read changing history.

Planning content expires after `AISMR_RETENTION_DAYS`. Expired records cannot be used as context. Admission and the existing Studio housekeeping operation remove expired request/result content and private revision payloads while retaining operation identities and cost records. This is application context, separate from coding-agent memory.

## Media recovery and accounting

A local download/decode failure after provider acceptance reuses that provider receipt. Collection permits three attempts, then stops with `media_retrieval_exhausted`. Safety rejection and moderation outage stop without regeneration. An unknown submission is never automatically repeated.

A new attempt after a definitive provider failure or rejection needs the configured retry allowance plus an exact positive reserved ledger entry. Video keys include scene ordinal and attempt; narration keys include attempt. Old runs without an attempt allocation cannot silently spend on another generation. Provider failure does not establish that the original request was free.

The new whole-run inventory covers configured media retries and every planning role in every allowed revision. `AISMR_COST_IDEATION_USD` is the configured reserve per planning call, sized for the selected role limits. The period budget remains the admission ceiling; this implementation does not choose or increase it. Uncertain planning operations retain holds and mark their ledger entry for reconciliation.

The UI counts latest video attempts as scenes. Its work telemetry counts provider jobs separately from locally split narration outputs. Plan progress uses saved step-started/completed events; a role is not displayed as completed merely because a spinner elapsed.

## Offline comparison

The comparison module reads saved results and makes no provider calls:

```sh
PYTHONPATH=src .venv/bin/python -m myloware.studio.planning_evaluation \
  --input comparison-input.json \
  --output blinded-review.json
```

Input contains `seed` and `records`. Each record supplies `variant` (`current`, `improved`, `four_role` by default), `object_id`, positive `replicate`, a saved versioned plan under `plan`, and optional `measurement` with observed `latency_seconds` and `cost_usd`. An optional `expected_variants` list supports a smaller comparison. The reader accepts immutable v1 archive plans and strict v2 scene plans. Obtain current-baseline outputs before substituting the improved prompt; the module does not recreate historical prompts.

The requested output is blinded content. A separate `.key.json` contains variant assignments, coverage, exact content duplicates, validation results and measured latency/cost summaries. Missing or invalid measurements remain unknown. Exact duplicate detection does not measure semantic similarity.

The proposed quality experiment is five objects, three repetitions and three variants (45 plan sets). Review full sets blindly for object fit, clarity, diversity, cross-run repetition and likely visible payoff. Adopt the extra roles only if preference and failure rates justify their measured cost and delay. No live preference result or real-video quality gain is established by the offline tests.

Fixture mode does not call the creative agents. Its sample plans can verify interaction and workflow state, but cannot evaluate originality, storyboard quality or the effect of prompt changes. Creative evaluation must invoke the actual selected planner backend and retain its requests, responses, model identity and usage receipts. Text-only planning evaluation does not establish generated-video quality or activate the public studio.
