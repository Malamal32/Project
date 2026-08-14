"""Deterministic company dedupe on (normalized_name, domain). Ambiguous cases (more
than one existing company matches) are logged for review rather than auto-merged —
a new row is created instead of guessing which candidate is the right match. A real
review queue lands in Phase 8 (`review_items`, entity_type='company_merge').
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.company import Company

log = structlog.get_logger()

_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|llc|l l c|ltd|limited|corp|corporation|co|company|plc)\.?\s*$",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_company_name(name: str) -> str:
    n = name.strip().lower()
    n = _NON_ALNUM_RE.sub(" ", n)
    n = _WHITESPACE_RE.sub(" ", n).strip()
    n = _SUFFIX_RE.sub("", n).strip()
    return n


def resolve_company(
    session: Session, canonical_name: str, domain: Optional[str], now: datetime
) -> Company:
    normalized = normalize_company_name(canonical_name)
    matches = list(session.execute(select(Company).where(Company.normalized_name == normalized)).scalars())

    if domain:
        exact = [c for c in matches if c.domain == domain]
        if exact:
            company = exact[0]
            company.last_seen_at = now
            return company

    if len(matches) == 1:
        company = matches[0]
        if domain and company.domain is None:
            company.domain = domain
        company.last_seen_at = now
        return company

    if len(matches) > 1:
        log.warning(
            "company_resolution.ambiguous_merge",
            canonical_name=canonical_name,
            normalized_name=normalized,
            candidate_ids=[str(c.company_id) for c in matches],
        )

    company = Company(
        canonical_name=canonical_name,
        normalized_name=normalized,
        domain=domain,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(company)
    session.flush()
    return company
