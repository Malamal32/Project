"""The migration DDL and the ORM metadata must describe the same schema.

`migrations/d1/*.sql` is what actually reaches D1, and `Base.metadata` is what the
pipeline writes through. Nothing enforces the link between them at runtime, so a
model change that never made it into a migration would pass every other test and
then fail — or worse, silently lose a column — at sync time. This test is that link.

If it fails, regenerate the DDL: `python -m scripts.generate_d1_schema`
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect

from models import Base
from pipeline.init_db import apply_migrations

# Created by the migration but not modelled in SQLAlchemy — a virtual table has no
# declarative equivalent. Its own coverage is in test_alt_title_fts.py.
FTS_OBJECTS = {"occupation_alt_titles_fts"}


def _engine_from_migrations(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migrated.sqlite3'}", future=True)
    raw = engine.raw_connection()
    try:
        apply_migrations(raw.driver_connection)
    finally:
        raw.close()
    return engine


def _engine_from_metadata(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'declared.sqlite3'}", future=True)
    Base.metadata.create_all(engine)
    return engine


def _real_tables(inspector) -> set[str]:
    return {
        name
        for name in inspector.get_table_names()
        # fts5 keeps its own shadow tables (_data, _idx, _content, ...) alongside the
        # virtual table; none of them are part of the declared schema.
        if name != "d1_migrations" and not any(name.startswith(f) for f in FTS_OBJECTS)
    }


def test_migration_and_metadata_declare_the_same_tables(tmp_path):
    migrated = inspect(_engine_from_migrations(tmp_path))
    declared = inspect(_engine_from_metadata(tmp_path))

    assert _real_tables(migrated) == _real_tables(declared)


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_migration_and_metadata_agree_on_columns(tmp_path, table):
    migrated = inspect(_engine_from_migrations(tmp_path))
    declared = inspect(_engine_from_metadata(tmp_path))

    def shape(inspector):
        return {
            (col["name"], str(col["type"]), bool(col["nullable"]))
            for col in inspector.get_columns(table)
        }

    assert shape(migrated) == shape(declared), (
        f"{table} differs between migrations/d1/ and models/ — "
        "regenerate with `python -m scripts.generate_d1_schema`"
    )


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_migration_and_metadata_agree_on_keys_and_indexes(tmp_path, table):
    migrated = inspect(_engine_from_migrations(tmp_path))
    declared = inspect(_engine_from_metadata(tmp_path))

    def keys(inspector):
        return (
            tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
            sorted(
                (idx["name"], tuple(idx["column_names"]), bool(idx["unique"]))
                for idx in inspector.get_indexes(table)
            ),
            sorted(
                (uc["name"], tuple(uc["column_names"]))
                for uc in inspector.get_unique_constraints(table)
            ),
        )

    assert keys(migrated) == keys(declared), (
        f"{table} keys/indexes differ between migrations/d1/ and models/ — "
        "regenerate with `python -m scripts.generate_d1_schema`"
    )


def test_foreign_keys_are_enforced(db_session):
    """D1 enforces foreign keys by default; the local database must too, or a
    violation would first surface during sync rather than during ingestion."""
    from sqlalchemy import text

    assert db_session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
