#!/bin/bash
set -e

echo "Running database migrations..."
alembic upgrade head

echo "Starting AISMR API..."
exec uvicorn myloware.api.server:app --host 0.0.0.0 --port 8000
