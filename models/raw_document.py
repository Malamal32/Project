from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base
from models.types import TZDateTime

DOC_TYPES = ("api_response", "posting_description", "html_page")


class RawDocument(Base):
    """Immutable landing zone for raw payloads. Rows are inserted, never updated —
    a changed payload (different hash) lands as a new row, never overwrites the old one.

    The payload *body* lives in object storage (R2), not in this table: D1 caps a row
    at 2 MB and a database at 10 GB, and raw job descriptions are the one thing here
    that grows without bound. `payload_r2_key` is content-addressed by
    `payload_sha256`, so a re-fetch of identical bytes rewrites the same key and the
    immutability contract holds by construction. Read a body back with
    `pipeline.raw_store.get()`.
    """

    __tablename__ = "raw_documents"
    __table_args__ = (
        CheckConstraint(f"doc_type IN {DOC_TYPES}", name="ck_raw_documents_doc_type"),
        UniqueConstraint("source_id", "url", "doc_type", "payload_sha256", name="uq_raw_documents_dedupe"),
    )

    raw_document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.source_id"), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(30), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    payload_r2_key: Mapped[str] = mapped_column(String(120), nullable=False)
    payload_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class RawDocumentRecord(BaseModel):
    """Validation boundary for a single raw_documents row before it is inserted."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source_id: uuid.UUID
    doc_type: Literal["api_response", "posting_description", "html_page"]
    url: str
    fetched_at: datetime
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    payload: str
    payload_sha256: str
    created_at: datetime
