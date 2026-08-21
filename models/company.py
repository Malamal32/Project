from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base
from models.types import TZDateTime


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        Index("ix_companies_normalized_name_domain", "normalized_name", "domain"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False)
    domain: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    hq_state: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    hq_city: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    naics_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    size_bucket: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class CompanyRecord(BaseModel):
    """Validation boundary for a single company row before it is upserted."""

    model_config = ConfigDict(str_strip_whitespace=True)

    canonical_name: str
    normalized_name: str
    domain: Optional[str] = None
    hq_state: Optional[str] = None
    hq_city: Optional[str] = None
    naics_code: Optional[str] = None
    size_bucket: Optional[str] = None
    first_seen_at: datetime
    last_seen_at: datetime
