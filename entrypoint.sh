#!/usr/bin/env bash
set -euo pipefail

# Apply DB migrations, then start the API server.
echo "Running alembic migrations..."
alembic upgrade head

echo "Starting uvicorn on port ${APP_PORT:-8100}..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${APP_PORT:-8100}"
