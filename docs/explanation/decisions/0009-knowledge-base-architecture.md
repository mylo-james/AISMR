# ADR-0009: Knowledge Base Architecture

**Status**: Accepted
**Date**: 2024-12-06

## Context

Agents need domain knowledge (platform specs, scripting patterns, editing conventions) to produce consistent outputs. We want:

- reusable knowledge shared across projects, and
- project-specific knowledge that doesn’t leak into other projects,
- retrieval that’s observable when quality regresses.

## Decision

### Two-tier knowledge base

- **Global KB**: `data/knowledge/` (shared across projects)
- **Project KB**: `data/projects/{project}/knowledge/` (project-specific voice, constraints, examples)

### Ingestion + chunking defaults

- Chunk size: ~512 tokens
- Chunk overlap: ~100 tokens
- Store metadata per chunk (source path, category, KB scope, section heading where available)

### Retrieval + observability

- Agents use `builtin::rag/knowledge_search` (OpenAI-compatible `file_search`) against the configured vector store.
- Retrieval logs include counts and top sources for debugging.

## Consequences

Positive:
- Shared knowledge improves consistency without duplicating documents.
- Project KB can override/extend global guidance safely.
- Retrieval regressions are debuggable via logs + metadata.

Negative:
- Two knowledge scopes to keep organized.
- Quality depends on document hygiene and periodic pruning.

## References

- `src/myloware/knowledge/loader.py`
- `src/myloware/knowledge/setup.py`
- `aismr kb setup --help`
