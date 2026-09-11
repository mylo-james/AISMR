# Quickstart

## Prerequisites

- Python **3.13** (see `.python-version`)
- Docker (optional, for running the stack locally)

## Install (dev)

```bash
python -m venv .venv
source .venv/bin/activate
make dev-install
```

## Configure

Create a local `.env`:

```bash
make demo-safe   # boots API safely (no background workflows)
# or
make demo-run    # enables background workflows (still fake providers by default)
```

## Run

Start the API server:

```bash
make serve
```

## 5-minute smoke test

Runs a full fake workflow end-to-end (no real APIs), including the HITL ideation approval via `/v2`.

```bash
make demo-smoke
```

Smoke test the CLI:

```bash
aismr --help
```

## Quality gates

```bash
make preflight
make ci
```

## Optional: run the full stack (Docker)

```bash
make docker-up
make docker-logs
```
