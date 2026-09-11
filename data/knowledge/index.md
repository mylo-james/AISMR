# Workflow agent knowledge

Reviewed 2026-09-10. Active references are deliberately small and role-scoped.

| Role | References |
| --- | --- |
| Ideator | ideation/concept-planning.md and the selected project's ideator references |
| Producer | production/asset-contract.md |
| Editor | editing/scene-rendering.md |
| Publisher | publishing/publish-to-tiktok.md |
| Supervisor | workflow/orchestration.md |

Every operational Markdown document declares YAML front matter with roles,
status: active and reviewed date. The local role_knowledge_search tool receives
its project and role from the factory, filters documents before searching, and
returns bounded text with source metadata. Untagged or inactive documents are
not returned. A project agent cannot request another role or a different project.

This is a deterministic local reference lookup, not a semantic vector search
or a persistent episodic-memory system. The old native vector-store setup,
maintenance and evaluation APIs remain available. Existing remote stores have
not been erased or reindexed; role agents do not read those stale shared stores.

Old references are preserved in docs/archive/knowledge-2026-09-09 and are outside
the ingested data/knowledge directories. They contain historical, unverified and
superseded advice and are not operational instructions.

Current code has separate legacy role-tool and deterministic scene-studio branches.
Active guidance names the owning branch and preserves scene-only media handoff.
Historical provider settings and calendar-shaped recipes remain legacy and are not
returned by role knowledge search. Refresh provider parameters and policies before
enabling a new adapter, and distinguish tested output from prompting hypotheses.
