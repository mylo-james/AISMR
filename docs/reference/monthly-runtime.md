# AISMR monthly runtime profile

The AISMR monthly workflow retains the existing Llama Stack client and role
runtime. The profile is intentionally narrow: one structured ideation call,
the current scoped knowledge/web-search tool surface, and a fail-closed text
safety shield. It does not use a saved Codex login or make Codex subscription
authentication available to visitors.

`src/myloware/config/monthly_runtime.py` is the offline contract. It defines:

- `openai/gpt-4o-mini` as the registered ideation model ID.
- `content_safety` as the Llama Guard shield ID.
- One model call, at most 6,000 input tokens, 2,400 output tokens, and three
  tool calls per ideation turn.
- A strict response schema with exactly twelve month ideas. T03 owns the
  canonical `MonthlyPlan` contract and may reuse this envelope.
- Four deterministic safety fixtures: allow, deny, malformed response, and
  outage. Only the explicit allow fixture passes. The other three fail closed.

`llama_stack/monthly-profile.yaml` registers the model and text shield for the
installed 0.3 client family. It is a candidate profile, not evidence that an
account, model, tool, or shield is available. It uses the same model and
`content_safety` Llama Guard resource shape already present in
`llama_stack/run-milvus.yaml`.

## Installed baseline and pin recommendation

The local implementation environment recorded during T02 has:

| Component | Observed version | T02 decision |
| --- | --- | --- |
| `llama-stack-client` | `0.3.5` | Keep within the existing `<0.4.0` family. |
| `langgraph` | `1.2.11` | Keep for this task. |
| `langgraph-checkpoint-postgres` | `3.1.2` | Preserve the existing Postgres path. |
| `langgraph-checkpoint-sqlite` | not installed | T08 should add and pin `==3.1.1`, then verify it against LangGraph 1.2.11 before replacing `MemorySaver`. |

LangGraph documents `langgraph-checkpoint-sqlite` as the maintained SQLite
backend and exposes `AsyncSqliteSaver` for async use. The package's published
3.1.1 release supports Python 3.13. T08 must make the dependency change,
initialize and close the saver with application lifecycle, enable strict
MessagePack handling or an explicit allowed-module list, and prove a
two-process resume. Until then, `MemorySaver` remains in-memory and does not
provide restart recovery.

Sources: [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence),
[LangGraph checkpointer integrations](https://docs.langchain.com/oss/python/integrations/checkpointers),
[SQLite checkpointer 3.1.1](https://pypi.org/project/langgraph-checkpoint-sqlite/),
and [GPT-4o mini](https://developers.openai.com/api/docs/models/gpt-4o-mini).

## Offline and live boundaries

The unit tests validate only the finite profile, strict schema envelope, and
fail-closed fixture handling. They make no network request and use no
credential. A later startup integration must call
`require_live_capability_checks` with observed values for all of the following:

1. `LLAMA_STACK_PROVIDER=real`.
2. Inference credentials configured without exposing the secret.
3. Llama Stack reachable.
4. `openai/gpt-4o-mini` registered and callable with structured output.
5. `content_safety` registered and callable.
6. The monthly allowlisted tools available.

T31, after separate live authorization, owns those checks and a real model and
shield smoke. T16 extends moderation to structured image verdicts and sampled
video frames. The text-only Llama Guard route here does not establish that
later image-capable requirement.

## Integration handoff

This task intentionally does not edit shared settings, Llama clients, the
agent factory, server startup, or dependencies. Root integration must:

1. Add a deployment-only selector that maps the monthly workflow to this
   profile and retains the current provider-mode behavior.
2. Wire profile limits and `allowed_tools` before constructing the ideator so
   the limits are enforced rather than descriptive.
3. Pass live observations into `require_live_capability_checks` during a real
   startup check. Do not infer success from fake mode or a saved Codex login.
4. Add `langgraph-checkpoint-sqlite==3.1.1` through the normal dependency
   workflow, then implement T08 separately.

The first real credential/model/shield call remains outside T02 authorization.
