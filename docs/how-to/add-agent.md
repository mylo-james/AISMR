# Add an Agent

> **How-To Guide**: This guide shows you how to create a new agent role. For the YAML schema reference, see [Agent Config](../reference/agent-config.md).

Create a new agent role for your workflow.

---

## Create the Config File

Agents are defined in YAML. Create `data/shared/agents/your_role.yaml`:

```yaml
role: your_role
description: What this agent does

model: meta-llama/Llama-3.2-3B-Instruct

instructions: |
  You are the [Role Name] for AISMR.

  ## Your Job
  [Clear description of responsibilities]

  ## Output Format
  [Expected output structure]

tools:
  - builtin::websearch
  - builtin::rag/knowledge_search

sampling_params:
  strategy:
    type: greedy
```

---

## Override for a Project

Create project-specific behavior in `data/projects/{project}/agents/your_role.yaml`:

```yaml
instructions: |
  [Project-specific instructions that override the base]
```

The factory merges base + project configs automatically.

---

## Use in Code

```python
from myloware.agents.factory import create_agent

agent = create_agent(
    client=client,
    project="your_project",
    role="your_role",
    vector_db_id="project_kb",  # optional: for RAG
)
```

---

## Add to Workflow

Edit `src/myloware/workflows/langgraph/nodes.py` to include your agent in the pipeline (and `src/myloware/workflows/langgraph/graph.py` if you add new nodes/edges).

---

## Test

```bash
pytest tests/unit/test_your_role.py -v
```
