"""Posting ingestion orchestrator: for each enabled ats_api source, discover ->
fetch -> parse via its Collector, then land raw documents, redact PII, classify
US scope, dedupe the company, and upsert the posting (versioning on content change).

Run as: python -m pipeline.ingest_postings [--source-name "Greenhouse: Stripe"]
Idempotent: re-running with unchanged upstream data only bumps last_seen_at.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog
import typer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from models.db import get_session
from models.posting import Posting, PostingVersion
from models.raw_document import RawDocument
from models.source import Source
from pipeline.collectors.base import Collector
from pipeline.collectors.greenhouse import GreenhouseCollector
from pipeline.company_resolution import resolve_company
from pipeline.pii_redaction import redact_pii, validate_redacted
from pipeline.politeness import PolitenessError, PolitenessGate
from pipeline.us_scope import classify_location

log = structlog.get_logger()
app = typer.Typer()

_EMPLOYMENT_TYPE_PATTERNS = [
    (re.compile(r"\bintern(ship)?\b", re.IGNORECASE), "internship"),
    (re.compile(r"\bpart[- ]?time\b", re.IGNORECASE), "part_time"),
    (re.compile(r"\b(contract|contractor|temp|temporary)\b", re.IGNORECASE), "contract"),
]


def infer_employment_type(title: str) -> Optional[str]:
    """Greenhouse's base API exposes no employment_type field. Only infer when the
    title gives an explicit cue; otherwise leave unspecified rather than defaulting
    to full_time and risking a wrong assertion."""
    for pattern, label in _EMPLOYMENT_TYPE_PATTERNS:
        if pattern.search(title):
            return label
    return None


def compute_content_hash(title_raw: str, description_text: str) -> str:
    h = hashlib.sha256()
    h.update(title_raw.encode("utf-8"))
    h.update(b"\x00")
    h.update(description_text.encode("utf-8"))
    return h.hexdigest()


def land_raw_document(
    session: Session,
    source_id: uuid.UUID,
    doc_type: str,
    url: str,
    payload: str,
    http_status: Optional[int],
    content_type: Optional[str],
    fetched_at: datetime,
) -> RawDocument:
    """Insert-or-fetch: identical (source_id, url, doc_type, payload) never produces
    a duplicate row; a changed payload always lands as a new, separate row — raw
    documents are never updated in place."""
    payload_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    stmt = (
        pg_insert(RawDocument)
        .values(
            raw_document_id=uuid.uuid4(),
            source_id=source_id,
            doc_type=doc_type,
            url=url,
            fetched_at=fetched_at,
            http_status=http_status,
            content_type=content_type,
            payload=payload,
            payload_sha256=payload_sha256,
            created_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(index_elements=["source_id", "url", "doc_type", "payload_sha256"])
        .returning(RawDocument.raw_document_id)
    )
    row = session.execute(stmt).first()
    if row is not None:
        session.flush()
        return session.get(RawDocument, row[0])

    existing = session.execute(
        select(RawDocument).where(
            RawDocument.source_id == source_id,
            RawDocument.url == url,
            RawDocument.doc_type == doc_type,
            RawDocument.payload_sha256 == payload_sha256,
        )
    ).scalar_one()
    return existing


def upsert_posting(
    session: Session,
    *,
    source_id: uuid.UUID,
    source_posting_id: str,
    company_id: uuid.UUID,
    title_raw: str,
    description_raw_ref: uuid.UUID,
    description_text_for_hash: str,
    employment_type: Optional[str],
    is_remote: Optional[bool],
    location_raw: Optional[str],
    location_city: Optional[str],
    location_state: Optional[str],
    location_country: Optional[str],
    us_scope_reason: str,
    posted_at: Optional[datetime],
    now: datetime,
) -> tuple[Posting, str]:
    content_hash = compute_content_hash(title_raw, description_text_for_hash)
    existing = session.execute(
        select(Posting).where(Posting.source_id == source_id, Posting.source_posting_id == source_posting_id)
    ).scalar_one_or_none()

    if existing is None:
        posting = Posting(
            source_id=source_id,
            source_posting_id=source_posting_id,
            company_id=company_id,
            title_raw=title_raw,
            description_raw_ref=description_raw_ref,
            employment_type=employment_type,
            is_remote=is_remote,
            location_raw=location_raw,
            location_city=location_city,
            location_state=location_state,
            location_country=location_country,
            us_scope_reason=us_scope_reason,
            posted_at=posted_at,
            first_seen_at=now,
            last_seen_at=now,
            status="active",
            content_hash=content_hash,
        )
        session.add(posting)
        session.flush()
        return posting, "new"

    existing.last_seen_at = now
    if existing.status != "active":
        existing.status = "active"
        existing.closed_at = None

    if existing.content_hash == content_hash:
        return existing, "unchanged"

    session.add(
        PostingVersion(
            posting_id=existing.posting_id,
            title_raw=existing.title_raw,
            description_raw_ref=existing.description_raw_ref,
            content_hash=existing.content_hash,
            recorded_at=now,
        )
    )
    existing.title_raw = title_raw
    existing.description_raw_ref = description_raw_ref
    existing.content_hash = content_hash
    existing.employment_type = employment_type
    existing.is_remote = is_remote
    existing.location_raw = location_raw
    existing.location_city = location_city
    existing.location_state = location_state
    existing.location_country = location_country
    existing.us_scope_reason = us_scope_reason
    existing.posted_at = posted_at
    return existing, "updated"


def build_collector(source: Source, gate: PolitenessGate) -> Optional[Collector]:
    if source.source_type == "ats_api" and "boards-api.greenhouse.io" in source.base_url:
        return GreenhouseCollector(source.source_id, source.base_url, source.politeness_config, gate)
    return None


def run_source(session: Session, source: Source, gate: PolitenessGate, limit: Optional[int] = None) -> dict:
    stats = {"seen": 0, "new": 0, "updated": 0, "unchanged": 0, "excluded_non_us": 0, "parse_failed": 0}

    collector = build_collector(source, gate)
    if collector is None:
        log.warning("ingest_postings.no_collector_for_source", source_name=source.name, base_url=source.base_url)
        return stats

    for stub in collector.discover():
        if limit is not None and stats["seen"] >= limit:
            break
        stats["seen"] += 1
        try:
            raw = collector.fetch(stub)
        except PolitenessError as exc:
            log.error("ingest_postings.blocked_by_robots", source_name=source.name, url=stub.url, error=str(exc))
            continue
        except httpx.HTTPError as exc:
            log.error("ingest_postings.fetch_failed", source_name=source.name, url=stub.url, error=str(exc))
            continue

        land_raw_document(
            session, source.source_id, "api_response", raw.url, raw.payload,
            raw.http_status, raw.content_type, raw.fetched_at,
        )

        record = collector.parse(raw)
        if record is None:
            stats["parse_failed"] += 1
            continue

        redacted_text, redaction_matches = redact_pii(record.description_text)
        validate_redacted(redacted_text)
        if redaction_matches:
            log.info(
                "ingest_postings.pii_redacted",
                source_name=source.name,
                source_posting_id=record.source_posting_id,
                count=len(redaction_matches),
                kinds=sorted({m.kind for m in redaction_matches}),
            )

        location = classify_location(record.location_raw)
        if not location.is_us:
            stats["excluded_non_us"] += 1
            log.debug(
                "ingest_postings.excluded_non_us",
                source_posting_id=record.source_posting_id,
                location_raw=record.location_raw,
                reason=location.reason,
            )
            continue

        now = datetime.now(timezone.utc)
        description_doc = land_raw_document(
            session, source.source_id, "posting_description", record.url, redacted_text,
            None, "text/plain", now,
        )
        company = resolve_company(session, record.company_name_raw, domain=None, now=now)

        _, outcome = upsert_posting(
            session,
            source_id=source.source_id,
            source_posting_id=record.source_posting_id,
            company_id=company.company_id,
            title_raw=record.title_raw,
            description_raw_ref=description_doc.raw_document_id,
            description_text_for_hash=redacted_text,
            employment_type=infer_employment_type(record.title_raw),
            is_remote=location.is_remote,
            location_raw=record.location_raw,
            location_city=location.location_city,
            location_state=location.location_state,
            location_country=location.location_country,
            us_scope_reason=location.reason,
            posted_at=record.posted_at,
            now=now,
        )
        stats[outcome] += 1

    session.commit()
    return stats


@app.command()
def main(
    source_name: Optional[str] = typer.Option(None, help="Run only this source by name"),
    limit_per_source: Optional[int] = typer.Option(
        None, help="Cap postings discovered per source (useful for a quick/demo run; omit for a full crawl)"
    ),
) -> None:
    session = get_session()
    gate = PolitenessGate()
    try:
        query = select(Source).where(Source.enabled == True, Source.source_type == "ats_api")  # noqa: E712
        if source_name:
            query = query.where(Source.name == source_name)
        sources = list(session.execute(query).scalars())

        log.info("ingest_postings.start", source_count=len(sources))
        totals = {"seen": 0, "new": 0, "updated": 0, "unchanged": 0, "excluded_non_us": 0, "parse_failed": 0}
        for source in sources:
            log.info("ingest_postings.source_start", source_name=source.name)
            stats = run_source(session, source, gate, limit=limit_per_source)
            log.info("ingest_postings.source_done", source_name=source.name, **stats)
            for key in totals:
                totals[key] += stats[key]
        log.info("ingest_postings.done", **totals)
    finally:
        gate.close()
        session.close()


if __name__ == "__main__":
    app()
