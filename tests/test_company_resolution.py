from datetime import datetime, timezone

from sqlalchemy import select

from models.company import Company
from pipeline.company_resolution import normalize_company_name, resolve_company


def test_normalize_strips_suffix_and_punctuation():
    assert normalize_company_name("Acme, Inc.") == "acme"
    assert normalize_company_name("Acme LLC") == "acme"
    assert normalize_company_name("Acme") == "acme"


def test_resolve_company_creates_new_when_none_exists(db_session):
    now = datetime.now(timezone.utc)
    company = resolve_company(db_session, "Acme Inc", domain=None, now=now)
    assert company.canonical_name == "Acme Inc"
    assert company.normalized_name == "acme"


def test_resolve_company_reuses_existing_by_normalized_name(db_session):
    now = datetime.now(timezone.utc)
    first = resolve_company(db_session, "Acme Inc.", domain=None, now=now)
    second = resolve_company(db_session, "Acme", domain=None, now=now)
    assert first.company_id == second.company_id


def test_resolve_company_ambiguous_merge_creates_new_row_instead_of_guessing(db_session):
    now = datetime.now(timezone.utc)
    a = Company(canonical_name="Acme A", normalized_name="acme", domain="a.com", first_seen_at=now, last_seen_at=now)
    b = Company(canonical_name="Acme B", normalized_name="acme", domain="b.com", first_seen_at=now, last_seen_at=now)
    db_session.add_all([a, b])
    db_session.flush()

    resolved = resolve_company(db_session, "Acme", domain=None, now=now)

    assert resolved.company_id not in {a.company_id, b.company_id}
    all_matching = db_session.execute(select(Company).where(Company.normalized_name == "acme")).scalars().all()
    assert len(all_matching) == 3
