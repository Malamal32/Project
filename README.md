# hiring-db-pipeline

US-scoped data pipeline and database answering: **"For major X, which companies are
hiring, and what credentials, skills, and experience do those postings ask for?"**

Built in phases (see `PROMPT.md` for the full spec). Each phase stops for review
before the next begins.

## Stack

Python 3.11+ (`uv`), PostgreSQL 16 + pgvector, SQLAlchemy 2.x + Alembic, Pydantic v2,
`httpx` + `tenacity`, `typer` CLI, `structlog`, `pytest`.

## Setup

### GitHub Codespaces / devcontainer

Open the repo in a Codespace (or "Reopen in Container" in VS Code) and everything
below is done for you: Python 3.12 + `uv`, a `pgvector/pgvector:pg16` Postgres
service with `vector` and `pg_trgm` enabled, a generated `.env`, `uv sync`, and
`alembic upgrade head`. Config lives in `.devcontainer/`.

### Local

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv isn't installed
uv sync

cp .env.example .env   # edit DATABASE_URL if needed

# Local Postgres (Debian/Ubuntu) — skip if pointing at an existing server:
sudo apt-get install postgresql-16 postgresql-16-pgvector
sudo pg_ctlcluster 16 main start
sudo -u postgres psql -c "CREATE ROLE hiring_db_pipeline WITH LOGIN PASSWORD 'hiring_db_pipeline' CREATEDB;"
sudo -u postgres psql -c "CREATE DATABASE hiring_db_pipeline OWNER hiring_db_pipeline;"
sudo -u postgres psql -d hiring_db_pipeline -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"

uv run alembic upgrade head
```

## Running a pipeline stage

Every stage is idempotent and runnable standalone:

```sh
uv run python -m pipeline.load_cip
```

## Tests

```sh
uv run pytest
```

Tests that touch the database use the `db_session` fixture in `tests/conftest.py`,
which creates a throwaway Postgres schema per test and drops it afterward — no
separate test database needed, just the same `DATABASE_URL`.

## Adding a new source adapter (Phase 3+)

Implement the `Collector` protocol (`discover`, `fetch`, `parse`) as a sibling module
under `pipeline/`; see the Greenhouse adapter once Phase 3 lands.

## Reference data provenance

Every file under `data/reference/` is documented in `data/reference/SOURCE.md`
(URL, retrieval date, SHA-256 hash).

## Credentials expected in `.env`

- `DATABASE_URL` — required.
- Licensed feed / ATS credentials, LLM provider key — added when Phase 3/4 begin.
