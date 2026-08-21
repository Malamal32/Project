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

## Posting ingestion (Phase 3)

```sh
uv run python -m pipeline.load_sources        # seed/refresh the sources table
uv run python -m pipeline.ingest_postings      # full crawl of all enabled ats_api sources
uv run python -m pipeline.ingest_postings --source-name "Greenhouse: Stripe" --limit-per-source 20
```

Both stages are idempotent and safe to re-run: `load_sources` upserts on `name`,
`ingest_postings` upserts on `(source_id, source_posting_id)` and only writes a new
`posting_versions` row when the content actually changed — re-running against
unchanged upstream data just bumps `last_seen_at`.

Every fetch goes through the shared politeness layer (`pipeline/politeness.py`):
a `robots.txt` check that hard-fails the fetch if disallowed, a per-domain rate
limiter, retry-with-backoff on 5xx/429/transport errors, and an identifying
User-Agent built from `PIPELINE_CONTACT_EMAIL`. Every posting description is run
through `pipeline/pii_redaction.py` before it's stored, and US-scope is decided
by `pipeline/us_scope.py` — both intentionally favor precision over recall
(if it's not confidently in scope, it's excluded, not guessed), and both record
*why* a decision was made (`us_scope_reason` on `postings`) rather than hiding it.

## Adding a new source adapter

Implement the `Collector` protocol (`discover`, `fetch`, `parse`) as a sibling module
under `pipeline/collectors/`; see `pipeline/collectors/greenhouse.py` for the
reference implementation. A career-page collector must additionally call
`pipeline.allowlist.require_allowlisted(domain)` before fetching — populate
`data/reference/career_page_allowlist.csv` first (it ships empty by design).
A licensed-feed adapter needs real credentials in `.env` before it can be enabled;
see `data/reference/SOURCE.md` for the currently-stubbed Handshake source.

## Reference data provenance

Every file under `data/reference/` is documented in `data/reference/SOURCE.md`
(URL, retrieval date, SHA-256 hash).

## Credentials expected in `.env`

- `DATABASE_URL` — required.
- `PIPELINE_CONTACT_EMAIL` — recommended before running Phase 3 collectors; used
  in the outbound User-Agent so a source operator can reach us if needed.
- Licensed feed credentials (e.g. Handshake), LLM provider key — added when a
  licensed-feed adapter or Phase 4/5 begin.
