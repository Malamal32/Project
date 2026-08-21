"""Round-trip tests for the three column types that changed shape moving off Postgres.

SQLite has no native UUID, no JSONB, and no timezone-aware timestamp. Each is now
emulated (`sa.Uuid`, `sa.JSON`, `models.types.TZDateTime`), and an emulation that
quietly returns the wrong Python type — a string instead of a UUID, a naive instead
of an aware datetime — would corrupt data in ways no other test would notice.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import StatementError

from models.company import Company
from models.source import Source


def _company(**overrides) -> Company:
    now = datetime.now(timezone.utc)
    defaults = dict(
        canonical_name="Acme Inc",
        normalized_name="acme",
        first_seen_at=now,
        last_seen_at=now,
    )
    return Company(**{**defaults, **overrides})


def test_uuid_round_trips_as_a_uuid_not_a_string(db_session):
    company = _company()
    db_session.add(company)
    db_session.commit()
    company_id = company.company_id
    db_session.expunge_all()

    loaded = db_session.get(Company, company_id)
    assert isinstance(loaded.company_id, uuid.UUID)
    assert loaded.company_id == company_id


def test_uuid_is_queryable_by_value(db_session):
    company = _company()
    db_session.add(company)
    db_session.commit()

    found = db_session.execute(
        select(Company).where(Company.company_id == company.company_id)
    ).scalar_one()
    assert found.canonical_name == "Acme Inc"


def test_datetime_round_trips_timezone_aware_in_utc(db_session):
    # A non-UTC input proves the value is normalized rather than just relabelled.
    tokyo = timezone(timedelta(hours=9))
    posted = datetime(2026, 3, 1, 9, 30, tzinfo=tokyo)

    company = _company(first_seen_at=posted, last_seen_at=posted)
    db_session.add(company)
    db_session.commit()
    company_id = company.company_id
    db_session.expunge_all()

    loaded = db_session.get(Company, company_id)
    assert loaded.first_seen_at.tzinfo is not None
    assert loaded.first_seen_at.utcoffset() == timedelta(0)
    assert loaded.first_seen_at == posted  # same instant, expressed in UTC


def test_naive_datetime_is_rejected_rather_than_guessed(db_session):
    """Postgres' TIMESTAMPTZ absorbed naive values; SQLite would store them as-is and
    silently misplace the instant. Failing loudly is the whole point of TZDateTime."""
    db_session.add(_company(first_seen_at=datetime(2026, 3, 1, 9, 30)))
    with pytest.raises(StatementError, match="naive datetime"):
        db_session.commit()


def test_json_round_trips_as_a_dict(db_session):
    config = {"crawl_delay_seconds": 1.5, "rate_limit_per_minute": 60, "nested": {"a": [1, 2]}}
    source = Source(
        source_type="ats_api",
        name="Test Source",
        base_url="https://example.test/board",
        auth_mode="none",
        enabled=True,
        politeness_config=config,
    )
    db_session.add(source)
    db_session.commit()
    db_session.expunge_all()

    loaded = db_session.execute(select(Source)).scalar_one()
    assert loaded.politeness_config == config
    assert isinstance(loaded.politeness_config["crawl_delay_seconds"], float)
