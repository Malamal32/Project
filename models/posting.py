from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base
from models.types import TZDateTime

POSTING_STATUSES = ("active", "stale", "closed")


class Posting(Base):
    __tablename__ = "postings"
    __table_args__ = (
        CheckConstraint(f"status IN {POSTING_STATUSES}", name="ck_postings_status"),
        UniqueConstraint("source_id", "source_posting_id", name="uq_postings_source_posting"),
        # Declared here rather than only in a migration so they reach D1 too. D1
        # bills on rows *scanned*, not returned, so an unindexed `status` filter
        # costs a full table scan on every analytical query.
        Index("ix_postings_company_id", "company_id"),
        Index("ix_postings_status", "status"),
    )

    posting_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.source_id"), nullable=False)
    source_posting_id: Mapped[str] = mapped_column(String(200), nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("companies.company_id"), nullable=False)
    title_raw: Mapped[str] = mapped_column(String(500), nullable=False)
    description_raw_ref: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("raw_documents.raw_document_id"), nullable=False
    )
    employment_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    is_remote: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    location_raw: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    location_city: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    location_state: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    location_country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    us_scope_reason: Mapped[str] = mapped_column(Text, nullable=False)
    posted_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class PostingVersion(Base):
    """A snapshot of a posting's head fields, written whenever content_hash changes.
    Preserves prior description text rather than silently overwriting it.
    """

    __tablename__ = "posting_versions"

    posting_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    posting_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("postings.posting_id"), nullable=False)
    title_raw: Mapped[str] = mapped_column(String(500), nullable=False)
    description_raw_ref: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("raw_documents.raw_document_id"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class PostingRecord(BaseModel):
    """Validation boundary for a single posting produced by a collector's parse() step."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source_posting_id: str
    company_name_raw: str
    title_raw: str
    description_text: str
    employment_type: Optional[str] = None
    is_remote: Optional[bool] = None
    location_raw: Optional[str] = None
    posted_at: Optional[datetime] = None
    url: str
