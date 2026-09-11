# Quickstart

> **Tutorial**: This guide walks you through getting AISMR running locally. By the end, you'll have created your first video workflow.

Get AISMR running locally in ~5 minutes (fake providers by default — no paid APIs required).

---

## Prerequisites

- Python **3.13** (see `.python-version`)
- Docker (optional; only needed for the “full stack” path)

---

## 1. Clone and Install

```bash
git clone https://github.com/mylo-james/AISMR.git
cd AISMR

python -m venv .venv
source .venv/bin/activate
make dev-install
```

---

## 2. Configure

```bash
make demo-safe   # safe defaults; no background workflows
# or
make demo-run    # enables background workflows (still fake providers by default)
```

This creates/updates a local `.env` (gitignored). The default demo mode uses fake providers so you can run everything without external services.

---

## 3. Run the API

```bash
make serve
```

---

## 4. Run an end-to-end fake workflow

```bash
make demo-smoke
```

---

## Optional: run the full stack (Docker)

```bash
make docker-up
make docker-logs
```

---

## Next Steps

- [Add an Agent](../how-to/add-agent.md) — Customize agent behavior
- [Architecture](../explanation/architecture.md) — Understand the system
