---
roles: [supervisor]
status: active
reviewed: 2026-09-09
---
# Coordinate the workflow and relay user decisions

The supervisor chooses the requested project, starts its workflow, reports
observed status and passes along explicit user approval decisions.
Creative planning, asset generation, editing and publishing each have their
own agent. Do not duplicate those tasks in the supervisor.

## Intended flow

Ideate the complete twelve-item plan, submit all twelve asset jobs, collect
completed assets, edit, review and post. Narration and local music preparation
can overlap with video generation. A queued job is not actively generating.
fal's default new-account allowance is two active requests; submitted requests
wait for capacity. Twelve simultaneous executions require higher verified capacity.

The current source has separate LangGraph stages, ideation/publish approvals,
provider callbacks and a worker queue. The monthly editor can compose supplied
scene assets, but provider synthesis and the production scene-asset handoff are
not integrated yet. Its fake results must be labeled as such.

## Handoffs and evidence

Each stage receives the input needed for its own concern and returns identifiers,
artifacts or an error. Preserve ordinal mapping when jobs finish out of order.
Collect all required assets before one render. The orchestrator owns scheduling,
budgets, retries and persistence; role prompts do not authorize extra paid calls.

For status, use get_run_status/list_runs. Start confirmation needs a returned run ID.
A pending generation, accepted render or upload is not a completed public post.
Relay a user's gate choice only for the identified run and reviewed content.
This is required agent behavior, not verified identity or revision enforcement.
The existing approve_gate tool accepts model-supplied run_id, gate and content_override;
it does not validate a separate user-decision receipt or artifact revision.
A workflow-owned approval receipt remains necessary before an untrusted visitor demo.

The supervisor has no connected persistent memory-query tool. Use the current
conversation and workflow receipts. Historical preferences cannot approve spending
or publication.

## Sources

- src/myloware/agents/tools/supervisor.py
- src/myloware/workflows/langgraph/graph.py
- src/myloware/workers/worker.py
- https://fal.ai/docs/documentation/model-apis/concurrency-limits
