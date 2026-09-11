# CLI Reference

Command-line interface for AISMR.

---

## Installation

The CLI is installed with the package:

```bash
pip install -e .
```

---

## Commands

### Run Server

```bash
uvicorn api.server:app --reload
```

Options:
- `--host`: Bind address (default: 127.0.0.1)
- `--port`: Port (default: 8000)
- `--reload`: Auto-reload on changes

---

### Database Migrations

```bash
# Apply migrations
alembic upgrade head

# Create new migration
alembic revision --autogenerate -m "description"

# Rollback one version
alembic downgrade -1
```

---

### Run Tests

```bash
# Unit tests
pytest tests/unit/ -v

# Integration tests (requires services)
pytest tests/integration/ -v

# With coverage
pytest tests/ --cov=src --cov-report=html
```

---

### Linting

```bash
# Check
ruff check src/ tests/

# Fix automatically
ruff check --fix src/ tests/

# Format
black src/ tests/
```

---

### Knowledge Base

```bash
# Validate knowledge documents
python scripts/validate_kb.py

# Setup memory bank
python scripts/setup_memory_bank.py
```

---

### Evaluation

```bash
# Run evaluation benchmark
python scripts/setup_eval_benchmark.py

# Setup evaluation dataset
python scripts/setup_eval_dataset.py
```

---

### Docker

```bash
# Start all services
docker compose up -d

# View logs
docker compose logs -f

# Stop services
docker compose down

# Rebuild after changes
docker compose build aismr
```
