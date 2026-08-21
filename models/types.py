"""Portable column types.

The pipeline targets SQLite locally and Cloudflare D1 remotely — the same dialect.
Neither has a native UUID type, a JSONB type, or a timezone-aware timestamp, so the
Postgres-specific types the schema used to declare are expressed here in terms
SQLAlchemy can render on either backend.

`Uuid` and `JSON` come straight from SQLAlchemy 2.0 and need no wrapper; only the
timestamp needs one, because SQLite silently discards `tzinfo`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class TZDateTime(TypeDecorator):
    """A timezone-aware timestamp that survives a backend without one.

    Values are normalized to UTC on the way in and stored naive; `tzinfo` is
    reattached as UTC on the way out, so application code only ever sees aware
    datetimes. Storing normalized UTC keeps the column lexicographically sortable
    and directly comparable in both SQLite and D1.

    Naive input is rejected rather than assumed to be UTC: guessing a timezone is
    exactly the kind of silent, unrecorded inference this pipeline avoids
    elsewhere, and `TIMESTAMPTZ` used to absorb the ambiguity for us.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                f"naive datetime {value!r} cannot be stored in a TZDateTime column; "
                "attach a timezone (datetime.now(timezone.utc)) at the point it is created"
            )
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)
