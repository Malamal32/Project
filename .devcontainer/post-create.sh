#!/usr/bin/env bash
# Runs once when the Codespace / devcontainer is created.
set -euo pipefail

DB_URL="postgresql+psycopg://hiring_db_pipeline:hiring_db_pipeline@db:5432/hiring_db_pipeline"

# models/db.py loads .env with override=True, so .env has to point at the compose
# service host ("db"), not localhost.
if [ ! -f .env ]; then
  sed "s|^DATABASE_URL=.*|DATABASE_URL=${DB_URL}|" .env.example > .env
  echo "Wrote .env (DATABASE_URL -> ${DB_URL})"
fi

uv sync

# Wait for Postgres to accept connections before migrating.
for _ in $(seq 1 30); do
  if uv run python -c "
import os, sqlalchemy
sqlalchemy.create_engine(os.environ['DATABASE_URL']).connect().close()
" 2>/dev/null; then
    break
  fi
  sleep 2
done

uv run alembic upgrade head

echo "Devcontainer ready. Try: uv run pytest"
