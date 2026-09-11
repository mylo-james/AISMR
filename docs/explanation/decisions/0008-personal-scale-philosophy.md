# ADR-0008: Personal Scale Philosophy

**Status**: Accepted
**Date**: 2024-12-06

## Context

AISMR is operated by a single maintainer. The workflow still has long-running steps (video generation, rendering, publishing) and external webhooks, so correctness and debuggability matter, but the infrastructure footprint should stay small.

## Decision

- Keep the deployment footprint minimal (API + workers + Postgres).
- Prefer correctness primitives over additional surface area:
  - fail-closed safety
  - webhook signature verification
  - idempotency + DB locking for webhook races
  - structured logging/tracing
- Defer multi-user auth and larger infrastructure until there is a concrete need.

## Consequences

Positive:
- Lower operational overhead and cost.
- Fewer moving parts while iterating.
- Scaling remains straightforward when needed (replicate API/worker, add shared media storage).

Negative:
- Single-user assumptions (API-key auth) limit scope.
- Some infrastructure work is done later instead of upfront.

## References

- `docs/explanation/scaling.md`
- `docs/explanation/decisions/0004-human-in-the-loop-gates.md`
