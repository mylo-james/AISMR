# AISMR Makefile
# Common commands for development workflow

.PHONY: help install dev-install test test-fast test-parity test-live lint format type-check ci docker-up docker-down clean eval openapi perf sbom e2e-local demo demo-safe demo-run demo-smoke security db-migrate db-migrate-sql db-reset
.PHONY: repo-scan preflight preflight-full

# Default knobs (override as needed)
HOST ?= 127.0.0.1
PORT ?= 8000
DEMO_UI_PORT ?= 8311
BASE_URL ?= http://localhost:8000
API_KEY ?= dev-api-key

# Default target
help:
	@echo "AISMR Development Commands"
	@echo "=============================="
	@echo ""
	@echo "Setup:"
	@echo "  make install       Install production dependencies"
	@echo "  make dev-install   Install with dev dependencies"
	@echo ""
	@echo "Quality:"
	@echo "  make lint          Run linter (ruff)"
	@echo "  make format        Format code (black + ruff)"
	@echo "  make type-check    Run type checker (mypy)"
	@echo "  make test          Run unit tests"
	@echo "  make test-cov      Run tests with coverage"
	@echo "  make ci            Run all CI checks locally"
	@echo "  make preflight     Run repo scan + CI + security + migrations"
	@echo "  make preflight-full  preflight + demo-smoke"
	@echo "  make eval          Run LLM-as-judge evaluation"
	@echo "  make eval-dry      Dry run (load dataset only)"
	@echo ""
	@echo "Demo:"
	@echo "  make demo-safe     Prepare .env (safe defaults; no background workflows)"
	@echo "  make demo-run      Update .env for runnable local demo (background workflows on)"
	@echo "  make demo-smoke    One-command end-to-end fake demo (starts server + completes run)"
	@echo "  make openapi       Export OpenAPI spec to openapi.json"
	@echo "  make perf          Run k6 perf smoke (/runs/start + webhooks)"
	@echo "  make e2e-local     In-process e2e (start → webhooks → artifacts)"
	@echo "  make sbom          Generate CycloneDX SBOM (sbom.json)"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-up     Start all services"
	@echo "  make docker-down   Stop all services"
	@echo "  make docker-logs   Follow service logs"
	@echo ""
	@echo "Observability:"
	@echo "  make watch-traces  Watch traces in console (polls Jaeger API)"
	@echo ""
	@echo "Database:"
	@echo "  make db-migrate    Run database migrations"
	@echo "  make db-reset      Reset database (WARNING: drops data)"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean         Remove build artifacts"

# =============================================================================
# Setup
# =============================================================================

install:
	pip install -e .

dev-install:
	pip install -e ".[dev]"
	@echo "Installing pre-commit hooks..."
	pre-commit install || echo "pre-commit not installed, skipping hooks"

demo: demo-safe

demo-safe:
	cp -n .env.example .env || true
	@echo "Demo env ready (.env) in SAFE mode (no background workflows)."

demo-run:
	cp -n .env.example .env || true
	python scripts/configure_demo.py
	@echo "Demo env updated to RUN mode (background workflows enabled, in-process dispatcher)."

demo-smoke:
	bash scripts/demo_smoke.sh

openapi:
	PYTHONPATH=src python scripts/generate_openapi.py

perf:
	k6 run -e BASE_URL=$(BASE_URL) -e API_KEY=$(API_KEY) scripts/perf/runs_and_webhooks.js

e2e-local:
	PYTHONPATH=src python scripts/e2e_local.py

security:
	bandit -c bandit.yaml -r src

sbom:
	python -m pip install -q "cyclonedx-bom>=4.0.0"
	cyclonedx-py -o sbom.json -F json

# =============================================================================
# Quality Checks
# =============================================================================

lint:
	ruff check src/ tests/

format:
	black src/ tests/
	ruff check --fix src/ tests/

type-check:
	mypy src/ --ignore-missing-imports

test:
	PYTHONPATH=src pytest tests/unit/ -v --tb=short

# Fast lane (default)
test-fast: test

# Parity lane (opt-in)
test-parity:
	RUN_PARITY=1 PYTHONPATH=src pytest -v --tb=short -m parity

# Live lane (manual)
test-live:
	PYTHONPATH=src pytest -v --tb=short -m live

test-cov:
	PYTHONPATH=src pytest tests/unit/ -v --tb=short --cov=src --cov-report=html --cov-report=term

test-integration:
	PYTHONPATH=src pytest tests/integration/ -v --tb=short -m "integration and not parity and not live"

# Run all CI checks locally
ci: lint type-check test
	@echo "✅ All CI checks passed!"

# Run evaluation pipeline (LLM-as-judge)
# Override threshold: EVAL_THRESHOLD=4.0 make eval
EVAL_THRESHOLD ?= 3.5
EVAL_DATASET ?= data/eval/ideator_test_cases.json

eval:
	PYTHONPATH=src python -m myloware.cli.main eval score --dataset $(EVAL_DATASET) --threshold $(EVAL_THRESHOLD)

eval-dry:
	PYTHONPATH=src python -m myloware.cli.main eval score --dataset $(EVAL_DATASET) --dry-run

# =============================================================================
# Docker
# =============================================================================

docker-up:
	docker compose up -d
	@echo "Services starting... Check status with 'docker compose ps'"

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

docker-build:
	docker compose build

# =============================================================================
# Database
# =============================================================================

db-migrate:
	PYTHONPATH=src alembic upgrade head

db-migrate-sql:
	PYTHONPATH=src alembic -c alembic.ini upgrade head --sql >/dev/null

db-reset:
	@echo "⚠️  This will DROP all data. Press Ctrl+C to cancel."
	@sleep 3
	PYTHONPATH=src alembic downgrade base
	PYTHONPATH=src alembic upgrade head

# =============================================================================
# Preflight Checks
# =============================================================================

repo-scan:
	bash scripts/repo_scan.sh

preflight: repo-scan ci security db-migrate-sql
	@echo "✅ Preflight checks passed!"

preflight-full: preflight demo-smoke
	@echo "✅ Preflight checks (full) passed!"

# =============================================================================
# Cleanup
# =============================================================================

clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf src/*.egg-info/
	rm -rf .pytest_cache/
	rm -rf .ruff_cache/
	rm -rf .mypy_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	rm -rf coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "✅ Cleaned build artifacts"

# =============================================================================
# Development Helpers
# =============================================================================

# Start API server in development mode
serve:
	PYTHONPATH=src uvicorn myloware.api.server:app --reload --host $(HOST) --port $(PORT)

# Run a single workflow (for testing)
run-workflow:
	@echo "Usage: make run-workflow PROJECT=motivational BRIEF='your brief'"
	PYTHONPATH=src python -c "from myloware.cli.main import cli; cli()" workflow start $(PROJECT) "$(BRIEF)"

# Watch Jaeger traces in console (Llama Stack native observability)
watch-traces:
	@echo "Watching Jaeger traces... (Ctrl+C to stop)"
	@python scripts/watch_traces.py

# Serve the existing static demo against the local API on port 8000.
.PHONY: demo-ui
demo-ui:
	python -m http.server $(DEMO_UI_PORT) --bind $(HOST) --directory web/demo
