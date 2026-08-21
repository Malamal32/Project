"""Coverage for the SQL that `pipeline.sync_d1` ships to Cloudflare D1.

D1's import path is stricter than plain SQLite: no transaction control statements,
and a hard 100 KB cap per statement. Both are silent-failure shaped — an oversized
statement or a stray COMMIT gets rejected partway through a load, leaving D1 holding
half a database. These tests check the generated file before it ever leaves the
machine.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

import pipeline.sync_d1 as sync_d1
from models.cip_code import CipCode
from models.source import Source

# D1's actual documented limit; MAX_STATEMENT_BYTES is our margin under it.
D1_STATEMENT_LIMIT = 100_000


@pytest.fixture()
def built_sql(db_session, tmp_path, monkeypatch):
    """Build the replace script against the test database."""
    monkeypatch.setattr(sync_d1, "engine", db_session.get_bind())
    path, counts = sync_d1.build_sql(tmp_path / "replace.sql")
    return path.read_text(encoding="utf-8"), counts


def _seed_many_rows(db_session, n: int) -> None:
    db_session.add_all([
        CipCode(
            cip_code=f"{i // 100:02d}.{i % 100:04d}",
            cip_title=f"Program {i} " + "x" * 200,  # pad so batching actually triggers
            level=6,
            is_active=True,
        )
        for i in range(n)
    ])
    db_session.commit()


def test_no_transaction_control_statements(db_session, built_sql):
    """D1 rejects BEGIN/COMMIT in an imported file."""
    sql, _ = built_sql
    for line in sql.splitlines():
        stripped = line.strip().upper()
        assert not stripped.startswith("BEGIN TRANSACTION")
        assert not stripped.startswith("COMMIT")


def test_defers_foreign_keys(db_session, built_sql):
    """Rows load a table at a time; without this, a child row would fail before its
    parent table is populated."""
    sql, _ = built_sql
    assert "PRAGMA defer_foreign_keys = true;" in sql.splitlines()[3]


def test_every_statement_fits_d1s_limit(db_session):
    _seed_many_rows(db_session, 2000)

    connection = db_session.get_bind().connect()
    try:
        statements = list(sync_d1._iter_insert_statements(connection, "cip_codes"))
    finally:
        connection.close()

    assert len(statements) > 1, "test data should be large enough to force batching"
    for statement in statements:
        assert len(statement.encode("utf-8")) <= sync_d1.MAX_STATEMENT_BYTES
        assert len(statement.encode("utf-8")) < D1_STATEMENT_LIMIT


def test_batched_inserts_reproduce_every_row(db_session, tmp_path, monkeypatch):
    """Round-trip: replaying the generated SQL into an empty database must give back
    exactly what was there."""
    _seed_many_rows(db_session, 500)
    db_session.add(Source(
        source_type="ats_api", name="S", base_url="https://x.test",
        auth_mode="none", enabled=True, politeness_config={"crawl_delay_seconds": 1.0},
    ))
    db_session.commit()

    monkeypatch.setattr(sync_d1, "engine", db_session.get_bind())
    path, counts = sync_d1.build_sql(tmp_path / "replace.sql")

    import sqlite3

    replayed = sqlite3.connect(tmp_path / "replayed.sqlite3")
    replayed.executescript(path.read_text(encoding="utf-8"))

    assert replayed.execute("SELECT count(*) FROM cip_codes").fetchone()[0] == 500
    assert counts["cip_codes"] == 500
    # JSON survives the literal round trip rather than arriving as a broken string.
    stored = replayed.execute("SELECT politeness_config FROM sources").fetchone()[0]
    assert "crawl_delay_seconds" in stored
    replayed.close()


def test_fts_index_is_rebuilt_not_dumped(db_session, built_sql):
    """FTS5 shadow tables cannot be carried across in a dump, so the index has to be
    regenerated from the table it indexes on the far side."""
    sql, _ = built_sql
    assert f"INSERT INTO {sync_d1.FTS_TABLE} (alt_title, onet_soc_code) SELECT" in sql
    # ...and the shadow tables must never appear as INSERT targets.
    for shadow in ("_data", "_idx", "_content", "_docsize", "_config"):
        assert f'INSERT INTO "{sync_d1.FTS_TABLE}{shadow}"' not in sql


def test_drop_order_is_child_before_parent(db_session, built_sql):
    """Dropping a parent first would fail against enforced foreign keys."""
    sql, _ = built_sql
    assert sql.index('DROP TABLE IF EXISTS "postings"') < sql.index('DROP TABLE IF EXISTS "companies"')
    assert sql.index('DROP TABLE IF EXISTS "posting_versions"') < sql.index('DROP TABLE IF EXISTS "postings"')


def test_string_literals_are_escaped(db_session, tmp_path, monkeypatch):
    db_session.add(CipCode(
        cip_code="01.0001",
        cip_title="O'Brien's \"Quoted\" Program -- not a comment",
        cip_definition="line1\nline2",
        level=6,
        is_active=True,
    ))
    db_session.commit()

    monkeypatch.setattr(sync_d1, "engine", db_session.get_bind())
    path, _ = sync_d1.build_sql(tmp_path / "replace.sql")

    import sqlite3

    replayed = sqlite3.connect(tmp_path / "replayed.sqlite3")
    replayed.executescript(path.read_text(encoding="utf-8"))
    title, definition = replayed.execute(
        "SELECT cip_title, cip_definition FROM cip_codes WHERE cip_code = '01.0001'"
    ).fetchone()
    assert title == "O'Brien's \"Quoted\" Program -- not a comment"
    assert definition == "line1\nline2"
    replayed.close()


def test_datetime_values_survive_the_round_trip(db_session, tmp_path, monkeypatch):
    """Timestamps are emitted as literals; they must come back as the same instant."""
    from models.company import Company

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    db_session.add(Company(
        canonical_name="Acme", normalized_name="acme", first_seen_at=now, last_seen_at=now,
    ))
    db_session.commit()
    expected = db_session.execute(text("SELECT first_seen_at FROM companies")).scalar_one()

    monkeypatch.setattr(sync_d1, "engine", db_session.get_bind())
    path, _ = sync_d1.build_sql(tmp_path / "replace.sql")

    import sqlite3

    replayed = sqlite3.connect(tmp_path / "replayed.sqlite3")
    replayed.executescript(path.read_text(encoding="utf-8"))
    assert replayed.execute("SELECT first_seen_at FROM companies").fetchone()[0] == expected
    replayed.close()
