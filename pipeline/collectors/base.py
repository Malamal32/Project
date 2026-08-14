"""The Collector protocol every source adapter implements. A licensed-feed or
career-page adapter is a drop-in sibling of the Greenhouse adapter as long as it
implements these three steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Protocol

from models.posting import PostingRecord


@dataclass(frozen=True)
class PostingStub:
    """A lightweight reference to a posting found in a source's listing —
    enough to fetch the full record."""

    source_posting_id: str
    url: str


@dataclass(frozen=True)
class RawDocument:
    """An in-memory raw fetch result, not yet landed in the raw_documents table."""

    url: str
    fetched_at: datetime
    http_status: int
    content_type: str
    payload: str


class Collector(Protocol):
    def discover(self) -> Iterable[PostingStub]:
        """List postings currently available from this source."""
        ...

    def fetch(self, stub: PostingStub) -> RawDocument:
        """Fetch the full raw payload for one posting."""
        ...

    def parse(self, raw: RawDocument) -> Optional[PostingRecord]:
        """Parse a raw payload into a validated PostingRecord."""
        ...
