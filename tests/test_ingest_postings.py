from datetime import datetime, timezone

from sqlalchemy import select

import pipeline.ingest_postings as ingest_postings
from pipeline import raw_store
from models.company import Company
from models.posting import Posting, PostingRecord, PostingVersion
from models.raw_document import RawDocument
from models.source import Source
from pipeline.collectors.base import PostingStub
from pipeline.collectors.base import RawDocument as CollectorRawDocument


class FakeCollector:
    """Bypasses the real Greenhouse HTTP round trip; discover/fetch/parse are driven
    entirely from an in-memory job list keyed by URL."""

    def __init__(self, jobs: list[dict]):
        self.jobs = jobs

    def discover(self):
        return [PostingStub(source_posting_id=j["id"], url=j["url"]) for j in self.jobs]

    def fetch(self, stub: PostingStub) -> CollectorRawDocument:
        return CollectorRawDocument(
            url=stub.url, fetched_at=datetime.now(timezone.utc),
            http_status=200, content_type="application/json", payload="{}",
        )

    def parse(self, raw: CollectorRawDocument) -> PostingRecord:
        job = next(j for j in self.jobs if j["url"] == raw.url)
        return PostingRecord(
            source_posting_id=job["id"],
            company_name_raw=job["company_name_raw"],
            title_raw=job["title"],
            description_text=job["description_text"],
            location_raw=job["location_raw"],
            posted_at=None,
            url=job["url"],
        )


def _make_source(db_session, name: str = "Test Source") -> Source:
    source = Source(
        source_type="ats_api", name=name, base_url="https://example.test/board",
        auth_mode="none", enabled=True, politeness_config={},
    )
    db_session.add(source)
    db_session.flush()
    return source


def _run(db_session, monkeypatch, jobs, source=None):
    source = source or _make_source(db_session)
    monkeypatch.setattr(ingest_postings, "build_collector", lambda src, gate: FakeCollector(jobs))
    return ingest_postings.run_source(db_session, source, gate=None), source


def test_new_posting_is_inserted_for_us_location(db_session, monkeypatch):
    jobs = [{
        "id": "1", "url": "https://example.test/jobs/1", "company_name_raw": "Acme Inc",
        "title": "Software Engineer", "description_text": "Build things.",
        "location_raw": "San Francisco, CA",
    }]
    stats, _ = _run(db_session, monkeypatch, jobs)

    assert stats == {"seen": 1, "new": 1, "updated": 0, "unchanged": 0, "excluded_non_us": 0, "parse_failed": 0}
    posting = db_session.execute(select(Posting)).scalar_one()
    assert posting.title_raw == "Software Engineer"
    assert posting.location_state == "CA"
    assert posting.status == "active"
    assert posting.us_scope_reason.startswith("city_state_pattern")


def test_non_us_location_is_excluded_and_not_stored(db_session, monkeypatch):
    jobs = [{
        "id": "1", "url": "https://example.test/jobs/1", "company_name_raw": "Acme Inc",
        "title": "Engineer", "description_text": "Build things.",
        "location_raw": "Dublin, Ireland",
    }]
    stats, _ = _run(db_session, monkeypatch, jobs)

    assert stats["excluded_non_us"] == 1
    assert db_session.execute(select(Posting)).scalars().first() is None


def test_rerun_with_unchanged_content_is_idempotent(db_session, monkeypatch):
    jobs = [{
        "id": "1", "url": "https://example.test/jobs/1", "company_name_raw": "Acme Inc",
        "title": "Engineer", "description_text": "Build things.",
        "location_raw": "Austin, TX",
    }]
    _, source = _run(db_session, monkeypatch, jobs)
    first_count = len(db_session.execute(select(Posting)).scalars().all())

    stats, _ = _run(db_session, monkeypatch, jobs, source=source)
    second_count = len(db_session.execute(select(Posting)).scalars().all())

    assert first_count == second_count == 1
    assert stats["unchanged"] == 1
    assert stats["new"] == 0


def test_content_change_writes_a_posting_version_and_never_mutates_old_raw_doc(db_session, monkeypatch):
    jobs = [{
        "id": "1", "url": "https://example.test/jobs/1", "company_name_raw": "Acme Inc",
        "title": "Engineer", "description_text": "Build things.",
        "location_raw": "Austin, TX",
    }]
    _, source = _run(db_session, monkeypatch, jobs)
    original = db_session.execute(select(Posting)).scalar_one()
    original_description_ref = original.description_raw_ref
    original_posting_id = original.posting_id

    jobs[0]["description_text"] = "Build many more things now."
    stats, _ = _run(db_session, monkeypatch, jobs, source=source)

    assert stats["updated"] == 1
    updated = db_session.execute(select(Posting)).scalar_one()
    assert updated.posting_id == original_posting_id
    assert updated.description_raw_ref != original_description_ref

    versions = db_session.execute(select(PostingVersion)).scalars().all()
    assert len(versions) == 1
    assert versions[0].description_raw_ref == original_description_ref
    assert versions[0].posting_id == original_posting_id

    # The body lives in the object store now; the row only points at it. The
    # original text must still be retrievable and unchanged.
    old_doc = db_session.get(RawDocument, original_description_ref)
    assert raw_store.get(old_doc.payload_r2_key) == "Build things."


def test_company_is_deduped_across_postings_from_the_same_source(db_session, monkeypatch):
    jobs = [
        {"id": "1", "url": "https://example.test/jobs/1", "company_name_raw": "Acme Inc.",
         "title": "Engineer", "description_text": "A.", "location_raw": "Austin, TX"},
        {"id": "2", "url": "https://example.test/jobs/2", "company_name_raw": "Acme Inc",
         "title": "Designer", "description_text": "B.", "location_raw": "Denver, CO"},
    ]
    _run(db_session, monkeypatch, jobs)

    companies = db_session.execute(select(Company)).scalars().all()
    postings = db_session.execute(select(Posting)).scalars().all()
    assert len(companies) == 1
    assert {p.company_id for p in postings} == {companies[0].company_id}


def test_pii_is_redacted_from_the_stored_description(db_session, monkeypatch):
    jobs = [{
        "id": "1", "url": "https://example.test/jobs/1", "company_name_raw": "Acme Inc",
        "title": "Engineer",
        "description_text": "Great role. Contact: Jane Smith or email jane@acme.com or call 415-555-0100.",
        "location_raw": "Austin, TX",
    }]
    _run(db_session, monkeypatch, jobs)

    posting = db_session.execute(select(Posting)).scalar_one()
    doc = db_session.get(RawDocument, posting.description_raw_ref)
    stored = raw_store.get(doc.payload_r2_key)
    assert "jane@acme.com" not in stored
    assert "555-0100" not in stored
    assert "Jane Smith" not in stored


def test_raw_api_response_is_landed_immutably(db_session, monkeypatch):
    jobs = [{
        "id": "1", "url": "https://example.test/jobs/1", "company_name_raw": "Acme Inc",
        "title": "Engineer", "description_text": "Build things.",
        "location_raw": "Austin, TX",
    }]
    _run(db_session, monkeypatch, jobs)

    api_docs = db_session.execute(select(RawDocument).where(RawDocument.doc_type == "api_response")).scalars().all()
    assert len(api_docs) == 1
