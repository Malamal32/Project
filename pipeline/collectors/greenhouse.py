"""Greenhouse job-board API collector — the one reference ATS adapter for Phase 3.

Public, documented, ToS-clean for job-board data:
https://developers.greenhouse.io/job-board.html

A `sources` row's `base_url` is expected to be a board's API root, e.g.
https://boards-api.greenhouse.io/v1/boards/{token}
"""

from __future__ import annotations

import html
import json
import re
import uuid
from datetime import datetime
from typing import Iterable, Optional

import structlog

from models.posting import PostingRecord
from pipeline.collectors.base import PostingStub, RawDocument
from pipeline.politeness import PolitenessGate

log = structlog.get_logger()

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_html(raw_html: str) -> str:
    # Greenhouse's `content` field is HTML-entity-escaped (e.g. "&lt;h2&gt;"), so
    # entities must be unescaped into real "<...>" tags *before* the tag regex can
    # see and strip them — doing it in the other order leaves literal tags in place.
    text = html.unescape(raw_html or "")
    text = _TAG_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def board_token_from_base_url(base_url: str) -> str:
    """https://boards-api.greenhouse.io/v1/boards/stripe -> 'stripe'"""
    return base_url.rstrip("/").rsplit("/", 1)[-1]


class GreenhouseCollector:
    """Implements the Collector protocol for a single company's Greenhouse job board."""

    def __init__(
        self,
        source_id: uuid.UUID,
        base_url: str,
        politeness_config: dict,
        gate: PolitenessGate,
    ) -> None:
        self.source_id = source_id
        self.base_url = base_url.rstrip("/")
        self.politeness_config = politeness_config
        self.gate = gate

    def discover(self) -> Iterable[PostingStub]:
        url = f"{self.base_url}/jobs?content=false"
        resp = self.gate.get(url, self.politeness_config)
        data = resp.json()
        for job in data.get("jobs", []):
            yield PostingStub(source_posting_id=str(job["id"]), url=job["absolute_url"])

    def fetch(self, stub: PostingStub) -> RawDocument:
        url = f"{self.base_url}/jobs/{stub.source_posting_id}"
        resp = self.gate.get(url, self.politeness_config)
        return RawDocument(
            url=url,
            fetched_at=datetime.now(),
            http_status=resp.status_code,
            content_type=resp.headers.get("content-type", ""),
            payload=resp.text,
        )

    def parse(self, raw: RawDocument) -> Optional[PostingRecord]:
        try:
            job = json.loads(raw.payload)
        except json.JSONDecodeError:
            log.warning("greenhouse.parse_failed", url=raw.url, reason="invalid_json")
            return None

        if not job.get("id") or not job.get("title"):
            log.warning("greenhouse.parse_failed", url=raw.url, reason="missing_required_field")
            return None

        return PostingRecord(
            source_posting_id=str(job["id"]),
            company_name_raw=job.get("company_name") or "",
            title_raw=job.get("title") or "",
            description_text=strip_html(job.get("content") or ""),
            employment_type=None,  # not exposed by the base Greenhouse job-board API
            is_remote=None,  # resolved from location_raw during ingestion, not here
            location_raw=(job.get("location") or {}).get("name"),
            posted_at=_parse_datetime(job.get("first_published")),
            url=job.get("absolute_url") or raw.url,
        )


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
