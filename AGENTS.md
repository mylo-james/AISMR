# AGENTS.md — AISMR

Short, actionable rules for assistants (Codex, Cursor, etc.).

## What to optimize for
- Keep LangGraph workflows, CLI, and safety intact; prefer small, reversible changes.
- Preserve HITL gates, shields, telemetry; avoid breaking KB ingest/eval paths.
- Minimize external calls in tests (use fakes); keep parity/live paths opt-in.

## Primary commands
- Setup: `make dev-install` (dev deps + pre-commit), or `make install` for prod deps.
- Quality gates: `make lint`, `make type-check`, `make test-fast` (default), `make test-parity` (Postgres), `make test-live` (real APIs, costly).
- Formatters/linters: Black + Ruff (`make format`), mypy (`make type-check`).
- KB: `myloware kb setup --help`, `myloware kb validate --help`.
- CLI smoke: `myloware --help` (or `PYTHONPATH=src python -m myloware.cli.main --help`).
- OpenAPI: `make openapi`; E2E: `make e2e-local`; Perf smoke: `make perf`.
- CLI entry points: `myloware ...` or `PYTHONPATH=src python -m myloware.cli.main ...`.
- Stack probes: `myloware stack status|models|shields|vector-dbs list`; chat smoke: `myloware stack chat "ping"`.

## Coding style
- Python 3.13; type hints required; prefer explicit imports.
- Run Ruff/Black before committing; fix lint at source, don’t blanket-# noqa.
- Handle network/API errors with clear `click.ClickException` or structured logs.
- Keep Rich/Click output concise; support `--json` where practical.
- Prefer dependency injection via functions over globals; avoid circular imports.
- Use `llama_clients.get_sync_client/get_async_client`; avoid new direct `LlamaStackClient` instantiations.
- Keep safety fail-closed; do not reintroduce “fail open”.

## Documentation voice (agent-written docs)
- Target tone: **engineer-to-engineer** (matter-of-fact, specific, assumes a technical reader).
- Prefer **concrete, falsifiable statements** (name files, symbols, commands) over “portfolio narrator” language.
- Avoid “AI tutor” voice (no “remember…”, no “the goal is…”, no motivational framing, no “as an AI…”).
- Keep narrator consistent:
  - If a doc is project/system documentation: write as **we/this project** (team voice).
  - If it’s explicitly personal context (e.g., a portfolio note): use **I** consistently.
  - Avoid third-person “the candidate” unless the document is clearly labeled as an evaluation artifact.
- Keep marketing minimal: no “validate signal”, “production-grade” unless backed by a concrete mechanism (tests, gates, checks).
- Use standard doc shapes:
  - **Reference**: what it is, configuration, interfaces, edge cases.
  - **How-to**: prerequisites, numbered steps, verification commands.
  - **Explanation**: why we chose X over Y, tradeoffs, constraints, links to ADRs.

## Tests
- Add/adjust tests under `tests/unit` for pure logic; `tests/integration` for DB/stack; mark with `parity`/`live` as appropriate.
- Fast lane is default: don’t break `make test-fast` (<4 min target).
- When touching CLI commands, add a small `CliRunner` test.
- Markers: `integration`, `parity` (Postgres), `live`; `fast_fake` auto-applied to unit tests; `fail_slow` used for budgeted durations.
- Parity/live runs expect real providers (`LLAMA_STACK_PROVIDER=real`, `SORA_PROVIDER=real`, `REMOTION_PROVIDER=real`, `UPLOAD_POST_PROVIDER=real`) and real services; fast lane uses fake providers + disables background workflows.

## Data, secrets, safety
- Never commit `.env`, API keys, or real secrets. Use `.env.example` patterns.
- Don’t remove or relax safety shields, HITL approvals, or content filters without explicit request.
- Treat `data/knowledge` as source-of-truth reference content—edit only if requested.

## Migrations & DB
- Use Alembic versions in `alembic/versions/`; never edit existing migrations—add new ones.
- `make db-migrate` / `make db-reset` are destructive; avoid running against prod URLs.

## CLI/stack specifics
- Llama Stack client is in `src/myloware/llama_clients.py`; prefer those helpers over direct `LlamaStackClient`.
- Keep CLI commands under `src/myloware/cli/main.py` grouped (runs, stack, kb, dev, traces).
- `stack shields` uses `client.shields.retrieve`; `stack vector-dbs register` supports provider/embedding/chunk flags—keep in sync with `kb setup`.

## Dependency changes
- Update `pyproject.toml`; avoid pin drift; prefer minimal version bumps.
- If adding runtime deps, justify and add to `requirements` equivalent; update README if needed.

## Logging & telemetry
- Use `myloware.observability.logging.get_logger`; avoid print except CLI UX.
- Don’t spam logs in hot paths; keep PII out of logs.

## PR / commit hygiene
- Keep changes scoped; summarize commands run.
- If tests aren’t run, state why and what to verify.

## When unsure
- Favor small PRs; add TODO with owner/date if deferring work.
- Ask before altering safety, billing-sensitive calls, or schema contracts.

## Repo map (high-yield places)
- `src/myloware/cli/main.py` — all CLI groups (runs/stack/kb/dev/traces); new commands must stay grouped.
- `src/myloware/llama_clients.py` — client factory, circuit breaker; use instead of raw clients.
- `src/myloware/config/settings.py` — env defaults/validators; safety is forced on.
- `src/myloware/workflows/langgraph` — main workflow graph/state; HITL gates live here.
- `src/myloware/observability` — telemetry/logging/eval helpers.
- `scripts/` — legacy utilities; prefer integrating into CLI when feasible.
- `docs/reference/env.md` — env descriptions; keep in sync when adding settings.

## KB and vector DB
- Default vector store name: `project_kb_{project}`; provider auto-selects Milvus if `MILVUS_URI` set else pgvector.
- CLI alignment: `myloware kb setup --provider-id ... --embedding-model ... --chunk-size ... --chunk-overlap ...` mirrors `stack vector-dbs register`.
- Keep chunk sizes sensible (defaults: 512/100 tokens approx).

## Workflows & safety
- HITL gates: ideation/publish; keep approval steps intact.
- Safety shields must stay enabled; fast-lane tests monkeypatch shields—don’t rely on that behavior in prod code.
- Webhooks: OpenAI Sora, Remotion, UploadPost—do not bypass signature checks.

## Logs & telemetry
- Use `myloware.observability.logging.get_logger`; CLI surfaces should prefer Rich for UX.
- Telemetry helpers: `myloware.observability.telemetry` (`query_run_traces`, etc.)—use for `runs monitor/logs`.
- Avoid printing secrets/PII; redact if logging errors with payloads.

## Efficiency tips
- Set `PYTHONPATH=src` when running ad-hoc scripts locally.
- For SQLite isolation in tests, respect fixtures; don’t hardcode DB URLs.
- Use `make watch-traces` or `myloware traces watch` for live trace debugging.
- For stuck runs: `myloware runs resume <uuid> --yes`; for artifacts/logs: `runs artifacts|logs`.
