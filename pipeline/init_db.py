"""Apply the D1 migrations to the local SQLite working database.

`migrations/d1/*.sql` is the single source of truth for the schema: the same files
are applied to Cloudflare D1 by `wrangler d1 migrations apply` and to the local
database by this stage. Applying the real DDL locally — rather than
`Base.metadata.create_all()` — is what guarantees the two agree, and it is the only
way the FTS5 virtual table gets created at all, since SQLAlchemy cannot model it.

Applied migrations are tracked in a `d1_migrations` table with the same shape
wrangler uses remotely, so `python -m pipeline.init_db` is safe to re-run and the
local and remote ledgers are directly comparable.

Run as: python -m pipeline.init_db [--reset]
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import structlog
import typer

from models.db import DATABASE_URL, engine

log = structlog.get_logger()
app = typer.Typer()

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "d1"

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS d1_migrations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE,
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def apply_migrations(connection: sqlite3.Connection, *, reset: bool = False) -> list[str]:
    """Apply any not-yet-applied migration files. Returns the names applied."""
    if reset:
        # Virtual tables must be dropped through their own DROP, and dropping in
        # reverse dependency order avoids tripping foreign keys.
        connection.executescript(
            "PRAGMA writable_schema = 1;"
            "DELETE FROM sqlite_master WHERE type IN ('table','index','trigger','view');"
            "PRAGMA writable_schema = 0;"
            "VACUUM;"
        )

    connection.execute(_LEDGER_DDL)
    already = {row[0] for row in connection.execute("SELECT name FROM d1_migrations")}

    applied: list[str] = []
    for path in migration_files():
        if path.name in already:
            continue
        connection.executescript(path.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO d1_migrations (name) VALUES (?)", (path.name,))
        connection.commit()
        applied.append(path.name)
        log.info("init_db.applied", migration=path.name)

    return applied


def init_local_db(*, reset: bool = False) -> list[str]:
    """Apply migrations to the engine configured in `models.db`."""
    db_path = Path(engine.url.database)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    raw = engine.raw_connection()
    try:
        applied = apply_migrations(raw.driver_connection, reset=reset)
    finally:
        raw.close()
    return applied


@app.command()
def main(
    reset: bool = typer.Option(False, help="Drop every existing object first, then re-apply from scratch"),
) -> None:
    if engine.dialect.name != "sqlite":
        raise typer.BadParameter(
            f"init_db targets the local SQLite database, but DATABASE_URL is {DATABASE_URL!r}. "
            "Remote schema changes go through `wrangler d1 migrations apply hiring-db --remote`."
        )

    applied = init_local_db(reset=reset)
    if applied:
        log.info("init_db.done", applied=applied, database=str(engine.url.database))
    else:
        log.info("init_db.up_to_date", database=str(engine.url.database))


if __name__ == "__main__":
    app()
