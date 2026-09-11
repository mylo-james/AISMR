# Agent configuration and role knowledge

Shared YAML lives in data/shared/agents; project overrides live in
data/projects/<project>/agents. Project instructions replace the shared instruction
string. The factory then appends its role contract, so project overrides cannot
erase the declared responsibility. This is an instruction boundary, not a guarantee
of model behavior. The tool allowlist enforces which effects the agent can request.

| Role | Concern | Owned tools |
| --- | --- | --- |
| ideator | concepts | role_knowledge_search, optional web research |
| producer | raw assets | sora_generate, role_knowledge_search |
| editor | text, composition and sound | remotion_render, inspect_render, role_knowledge_search |
| publisher | reviewed post submission | upload_post, role_knowledge_search |
| supervisor | routing, status and explicit user decisions | start_workflow, get_run_status, list_runs, approve_gate, role_knowledge_search |

The monthly renderer adds on-screen text and accepts supplied narration/music assets.
It does not implement the planned Wan, Kokoro synthesis or publisher migration. Model overrides retain their existing behavior.
Existing approval gates and safety checks remain in the workflow and its tools.
This cleanup does not add caller identity, a user-decision receipt or artifact revision
binding to approve_gate. Its supervisor-facing tool remains model-callable with a
content_override. The role instruction to relay an explicit user decision is advisory;
it is not an authorization barrier for an untrusted visitor demo.

## Source-owned reference documents

Active Markdown in data/knowledge and the selected project's knowledge directory
declares front matter:

```yaml
---
roles: [producer]
status: active
reviewed: 2026-09-09
---
```

role_knowledge_search fixes project/role at construction. It selects active documents
for that role before matching query terms and returns bounded excerpts with source
metadata. It is local lexical lookup, not semantic search or episodic memory.
Untagged, draft and legacy references are excluded from agent results. Query arguments
cannot select a different role, project or arbitrary path. Keep documents concise
so a role can retrieve the relevant contract without unrelated context.

The low-level file_search helpers and vector-store ingestion/maintenance/evaluation
APIs remain available. The product factories translate the old configured
builtin::rag/knowledge_search name to the scoped local reference tool. They do not
grant unrestricted file_search. Existing remote stores have not been reindexed.

Native filter fields exist in the installed client schema, but current uploads do
not preserve role attributes and provider enforcement is unverified. Local filtering
avoids claiming isolation from that unverified path. Reintroduce native scoped retrieval
only after per-file role attributes and a real cross-role retrieval test pass.

The supervisor does not have a persistent memory-query tool. Chat's legacy preference
storage is separate and unchanged; it is not injected into production agents.
Archived knowledge in docs/archive/knowledge-2026-09-09 is outside active ingestion.
