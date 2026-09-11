# ADR-0003: Config-Driven Agent Architecture

**Status**: Accepted
**Date**: 2024-12-06

## Context

AISMR has multiple agents (Ideator, Producer, Editor, Publisher) that need:
- Different instructions per project (AISMR vs. motivational)
- Shared base patterns (tools, shields)
- No code changes to add new projects

Options: hardcoded Python, YAML configs, or runtime generation.

## Decision

**YAML configuration with inheritance.** Base configs in `data/shared/`, project overrides in `data/projects/{project}/`.

```
data/
├── shared/agents/
│   ├── ideator.yaml      # Base: tools, shields, model
│   └── editor.yaml
└── projects/aismr/agents/
    └── ideator.yaml      # Override: instructions, model
```

Loading: base config → deep merge with project config → project wins.

Example:
```yaml
# data/shared/agents/ideator.yaml
role: ideator
model: meta-llama/Llama-3.2-3B-Instruct
tools: [builtin::websearch, builtin::rag/knowledge_search]
shields:
  input: [llama_guard]
  output: [llama_guard]

# data/projects/aismr/agents/ideator.yaml
instructions: |
  You are a creative ideator for ASMR video production.
  Generate calming, visually appealing concepts...
```

## Consequences

### Positive

- Separation of concerns (persona vs. infrastructure)
- Base configs shared across projects
- New projects = new YAML, no Python changes
- Version controlled in git

### Negative

- Indirection (multiple files to understand full config)
- Deep merge can be surprising
- No compile-time validation

### Neutral

- Minimal learning curve
- ~10ms config loading overhead (acceptable)

## Alternatives Rejected

| Option | Why Not |
|--------|---------|
| **Pure Python** | Changes require deployment. No clear separation. |
| **Database configs** | Overkill. Harder to version control. |

## Implementation

```python
# src/config/loaders.py
def load_agent_config(project: str, role: str) -> dict:
    base = load_yaml(f"data/shared/agents/{role}.yaml")
    override = load_yaml(f"data/projects/{project}/agents/{role}.yaml")
    return deep_merge(base, override)
```

## References

- [Twelve-Factor App: Config](https://12factor.net/config)
