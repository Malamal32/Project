"""Publish the local SQLite working database to Cloudflare D1.

The pipeline writes to a local SQLite file because it needs real transactions — the
`postings` upsert snapshots the previous head into `posting_versions` before mutating
it, and D1's HTTP API commits each statement independently. This stage takes the
finished database and loads it into D1 through the supported bulk path:
`wrangler d1 execute --file`.

The emitted SQL is shaped to D1's constraints, which differ from plain SQLite:

- No `BEGIN TRANSACTION` / `COMMIT` — D1 rejects them on import.
- `PRAGMA defer_foreign_keys` up front, so rows can load in any table order.
- Every statement kept under D1's 100 KB statement limit by batching INSERT rows.
- FTS5 shadow tables are skipped and the index is rebuilt with a plain INSERT ...
  SELECT on the far side; virtual tables cannot be carried across in a dump.

The load is a full replace rather than an incremental diff. At this corpus size
(~70k rows) replace is both simpler and safe, and it means a sync can never leave D1
holding a partial merge of two different pipeline runs.

Run as: python -m pipeline.sync_d1 [--dry-run] [--local]
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterator, Optional

import structlog
import typer
from sqlalchemy import text

from models import Base
from models.db import engine

log = structlog.get_logger()
app = typer.Typer()

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations" / "d1"
BUILD_DIR = REPO_ROOT / "data" / "d1_sync"

# D1 caps a single SQL statement at 100 KB. Stay well under it: the batcher measures
# the real statement as it builds, and this is the ceiling it will not cross.
MAX_STATEMENT_BYTES = 80_000

# Written by pipeline.init_db / wrangler; each side keeps its own ledger.
LEDGER_TABLE = "d1_migrations"

FTS_TABLE = "occupation_alt_titles_fts"


def _table_load_order() -> list[str]:
    """Tables in foreign-key-safe insert order (parents before children)."""
    return [t.name for t in Base.metadata.sorted_tables]


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _statement_bytes(prefix: str, batch: list[str]) -> int:
    """Exact byte length of the statement `_render` would produce for this batch."""
    return (
        len(prefix.encode("utf-8"))
        + sum(len(t.encode("utf-8")) for t in batch)
        + 2 * (len(batch) - 1)  # ",\n" between tuples
        + 1  # trailing ";"
    )


def _iter_insert_statements(connection, table: str) -> Iterator[str]:
    """Emit multi-row INSERTs for `table`, each within MAX_STATEMENT_BYTES."""
    columns = [row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))]
    if not columns:
        return

    column_list = ", ".join(f'"{c}"' for c in columns)
    prefix = f'INSERT INTO "{table}" ({column_list}) VALUES '

    def render(batch: list[str]) -> str:
        return prefix + ",\n".join(batch) + ";"

    batch: list[str] = []
    for row in connection.execute(text(f'SELECT {column_list} FROM "{table}"')):
        tuple_sql = "(" + ", ".join(_sql_literal(v) for v in row) + ")"

        if batch and _statement_bytes(prefix, batch + [tuple_sql]) > MAX_STATEMENT_BYTES:
            yield render(batch)
            batch = []

        # A single row too large to batch still has to go somewhere. Emit it alone
        # and say so rather than dropping it — D1 will reject it at 100 KB, and a
        # silent truncation here would look like a successful sync.
        if not batch and _statement_bytes(prefix, [tuple_sql]) > MAX_STATEMENT_BYTES:
            log.warning(
                "sync_d1.oversized_row",
                table=table,
                bytes=_statement_bytes(prefix, [tuple_sql]),
                limit=MAX_STATEMENT_BYTES,
            )

        batch.append(tuple_sql)

    if batch:
        yield render(batch)


def _schema_sql() -> str:
    """The checked-in migration DDL, which is what defines the remote schema."""
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS_DIR.glob("*.sql")))


def _drop_sql() -> str:
    """DROP every object currently in the remote schema, so the load starts clean.

    Derived from the ORM metadata rather than hardcoded, so a new table reaches this
    list without anyone remembering to update it. Children drop before parents, which
    matters because D1 enforces foreign keys.
    """
    statements = [f'DROP TABLE IF EXISTS "{FTS_TABLE}";']
    for table in reversed(_table_load_order()):
        statements.append(f'DROP TABLE IF EXISTS "{table}";')
    statements.append(f'DROP TABLE IF EXISTS "{LEDGER_TABLE}";')
    return "\n".join(statements)


def build_sql(output: Path) -> tuple[Path, dict[str, int]]:
    """Write the full replace script. Returns the path and per-table row counts."""
    output.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    with engine.connect() as connection, output.open("w", encoding="utf-8") as fh:
        fh.write("-- GENERATED by pipeline/sync_d1.py — do not edit.\n")
        fh.write("-- Full replace of the hiring-db D1 database from the local SQLite build.\n\n")
        # D1 rejects BEGIN/COMMIT in an imported file; defer_foreign_keys is what
        # makes a table-at-a-time load safe without them.
        fh.write("PRAGMA defer_foreign_keys = true;\n\n")

        fh.write("-- 1. Drop existing objects\n")
        fh.write(_drop_sql() + "\n\n")

        fh.write("-- 2. Recreate the schema from migrations/d1/\n")
        fh.write(_schema_sql() + "\n\n")

        fh.write("-- 3. Load data\n")
        for table in _table_load_order():
            count = connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
            counts[table] = count
            if count == 0:
                continue
            fh.write(f"\n-- {table} ({count} rows)\n")
            for statement in _iter_insert_statements(connection, table):
                fh.write(statement + "\n")

        # 4. FTS5 cannot be dumped — rebuild it on the far side from the table it
        # indexes, exactly as pipeline/load_occupations.py does locally.
        fts_count = connection.execute(text(f"SELECT count(*) FROM {FTS_TABLE}")).scalar_one()
        counts[FTS_TABLE] = fts_count
        fh.write(f"\n-- 4. Rebuild the FTS5 index ({fts_count} rows)\n")
        fh.write(
            f"INSERT INTO {FTS_TABLE} (alt_title, onet_soc_code) "
            "SELECT alt_title, onet_soc_code FROM occupation_alt_titles;\n"
        )

    return output, counts


def _wrangler(args: list[str], *, account_id: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLOUDFLARE_ACCOUNT_ID": account_id}
    return subprocess.run(
        ["wrangler", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _d1_query(database: str, account_id: str, sql: str, *, local: bool) -> list[dict]:
    result = _wrangler(
        ["d1", "execute", database, "--local" if local else "--remote", "--json", "--command", sql],
        account_id=account_id,
    )
    if result.returncode != 0:
        raise RuntimeError(f"wrangler d1 execute failed:\n{result.stderr or result.stdout}")

    import json

    # wrangler prefixes the JSON payload with human-readable banner lines.
    payload = json.loads(result.stdout[result.stdout.index("[") :])
    return payload[0]["results"]


def remote_counts(database: str, account_id: str, tables: list[str], *, local: bool) -> dict[str, int]:
    """Read row counts back out of D1 so the sync can be verified, not assumed.

    One query per table rather than a single UNION ALL: D1 caps the number of terms
    in a compound SELECT well below stock SQLite, and a dozen tables is already
    enough to trip it (`too many terms in compound SELECT`, code 7500).
    """
    counts: dict[str, int] = {}
    for table in tables:
        rows = _d1_query(database, account_id, f'SELECT count(*) AS n FROM "{table}"', local=local)
        counts[table] = rows[0]["n"]
    return counts


@app.command()
def main(
    dry_run: bool = typer.Option(False, help="Build the SQL file but do not touch D1"),
    local: bool = typer.Option(False, help="Apply to wrangler's local D1 emulator instead of the real database"),
    database: Optional[str] = typer.Option(None, help="D1 database name (defaults to $D1_DATABASE_NAME)"),
) -> None:
    database = database or os.environ.get("D1_DATABASE_NAME", "hiring-db")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not account_id and not dry_run:
        raise typer.BadParameter("CLOUDFLARE_ACCOUNT_ID is not set; add it to .env")

    log.info("sync_d1.build_start", database=database)
    path, counts = build_sql(BUILD_DIR / "replace.sql")
    size_mb = path.stat().st_size / 1_048_576
    log.info("sync_d1.build_done", path=str(path), size_mb=round(size_mb, 2), **counts)

    if dry_run:
        log.info("sync_d1.dry_run", message="built SQL only; D1 untouched")
        return

    log.info("sync_d1.apply_start", database=database, remote=not local)
    result = _wrangler(
        ["d1", "execute", database, "--local" if local else "--remote", "--yes", "--file", str(path)],
        account_id=account_id,
    )
    if result.returncode != 0:
        log.error("sync_d1.apply_failed", stderr=result.stderr[-4000:], stdout=result.stdout[-4000:])
        raise typer.Exit(1)
    log.info("sync_d1.apply_done")

    # Verify rather than assume: compare what D1 actually holds against the local
    # build, and fail loudly on any mismatch.
    tables = _table_load_order() + [FTS_TABLE]
    remote = remote_counts(database, account_id, tables, local=local)
    mismatches = {t: (counts[t], remote.get(t)) for t in tables if counts[t] != remote.get(t)}

    log.info("sync_d1.verified", **{t: remote.get(t) for t in tables})
    if mismatches:
        log.error("sync_d1.count_mismatch", mismatches=mismatches)
        raise typer.Exit(1)
    log.info("sync_d1.done", database=database, tables=len(tables))


if __name__ == "__main__":
    app()
