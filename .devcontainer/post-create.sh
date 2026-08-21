#!/usr/bin/env bash
# Runs once when the Codespace / devcontainer is created.
set -euo pipefail

# The pipeline writes to a local SQLite file — no database service to wait for.
# .env.example already points DATABASE_URL at it.
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Wrote .env from .env.example"
fi

uv sync

# Create data/hiring_db.sqlite3 and apply migrations/d1/*.sql — the same DDL that
# ships to Cloudflare D1.
uv run python -m pipeline.init_db

echo "Devcontainer ready. Try: uv run pytest"
