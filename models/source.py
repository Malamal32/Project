from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Boolean, CheckConstraint, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base

SOURCE_TYPES = ("licensed_feed", "ats_api", "career_page")


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(f"source_type IN {SOURCE_TYPES}", name="ck_sources_source_type"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    auth_mode: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    politeness_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    terms_reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceRecord(BaseModel):
    """Validation boundary for a single source row before it is upserted."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source_type: Literal["licensed_feed", "ats_api", "career_page"]
    name: str
    base_url: str
    auth_mode: str
    enabled: bool = True
    politeness_config: dict = {}
    terms_reviewed_at: Optional[datetime] = None
