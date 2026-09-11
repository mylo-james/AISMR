# AISMR Documentation

Reviewed surreal video creation with LangGraph. Start with the
[local AISMR studio](reference/aismr-local.md) or
[current deployment preparation](how-to/aismr-deployment.md).
The lower-level Llama Stack references document retained integrations.

This documentation follows the [Diátaxis](https://diataxis.fr/) framework.

---

## Tutorials

*Learning-oriented: guided lessons that take you through a series of steps.*

| Tutorial | What You'll Learn |
|----------|-------------------|
| [Quickstart](tutorials/quickstart.md) | Get AISMR running and create your first video workflow |

---

## How-To Guides

*Task-oriented: practical steps to solve specific problems.*

| Guide | Problem It Solves |
|-------|-------------------|
| [Add an Agent](how-to/add-agent.md) | Create a new agent role with custom behavior |
| [Add a Tool](how-to/add-tool.md) | Build custom Llama Stack tools for external services |
| [Deploy](how-to/deploy.md) | Vercel frontend, Cloudflare DNS, Neon and persistent video runtime |
| [Troubleshooting](how-to/troubleshooting.md) | Debug common issues |

---

## Reference

*Information-oriented: technical descriptions of the machinery.*

| Reference | What It Describes |
|-----------|-------------------|
| [API Endpoints](reference/api.md) | REST API routes, request/response schemas |
| [Environment Variables](reference/env.md) | Configuration options and defaults |
| [Agent Config Schema](reference/agent-config.md) | YAML format for agent definitions |
| [CLI Commands](reference/cli.md) | Command-line interface usage |

---

## Explanation

*Understanding-oriented: background, context, and reasoning.*

| Topic | What It Explains |
|-------|------------------|
| [Why Llama Stack](explanation/why-llama-stack.md) | The v1 → v2 evolution and architectural decisions |
| [Architecture](explanation/architecture.md) | System design, data flow, component relationships |
| [Llama Stack Integration](explanation/llama-stack.md) | How we use the Llama Stack SDK |
| [Safety](explanation/safety.md) | Content moderation approach and fail-closed design |
| [ADRs](explanation/decisions/) | Architecture Decision Records (12 documented decisions) |

---

## About Diátaxis

This documentation is organized according to the [Diátaxis framework](https://diataxis.fr/):

- **Tutorials** are *learning-oriented* — they guide you through steps to learn concepts
- **How-To Guides** are *task-oriented* — they help you accomplish specific goals
- **Reference** is *information-oriented* — it describes the technical machinery
- **Explanation** is *understanding-oriented* — it provides background and context

When contributing documentation, place new content in the appropriate category based on its purpose, not its topic.
