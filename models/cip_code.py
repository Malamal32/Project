from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base

CIP_CODE_PATTERNS = {
    2: r"^\d{2}$",
    4: r"^\d{2}\.\d{2}$",
    6: r"^\d{2}\.\d{4}$",
}


class CipCode(Base):
    __tablename__ = "cip_codes"

    cip_code: Mapped[str] = mapped_column(String(9), primary_key=True)
    cip_title: Mapped[str] = mapped_column(String(500), nullable=False)
    cip_definition: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_cip_code: Mapped[Optional[str]] = mapped_column(
        String(9), ForeignKey("cip_codes.cip_code"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    crosswalk_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    children: Mapped[list["CipCode"]] = relationship(
        "CipCode", back_populates="parent"
    )
    parent: Mapped[Optional["CipCode"]] = relationship(
        "CipCode", back_populates="children", remote_side=[cip_code]
    )


class CipCodeRecord(BaseModel):
    """Validation boundary for a single CIP code row before it is upserted."""

    model_config = ConfigDict(str_strip_whitespace=True)

    cip_code: str
    cip_title: str
    cip_definition: Optional[str] = None
    level: Literal[2, 4, 6]
    parent_cip_code: Optional[str] = None
    is_active: bool = True
    crosswalk_notes: Optional[str] = None

    @field_validator("cip_code")
    @classmethod
    def validate_cip_code_format(cls, v: str, info) -> str:
        # level isn't available yet during this field's validation (fields validate
        # in declaration order); the full cross-field format check runs in
        # `validate_format_matches_level` below.
        if not v or not v[0].isdigit():
            raise ValueError(f"cip_code must start with a digit, got {v!r}")
        return v

    @field_validator("level")
    @classmethod
    def validate_format_matches_level(cls, v: int, info) -> int:
        import re

        cip_code = info.data.get("cip_code")
        if cip_code is not None:
            pattern = CIP_CODE_PATTERNS[v]
            if not re.match(pattern, cip_code):
                raise ValueError(
                    f"cip_code {cip_code!r} does not match expected format "
                    f"{pattern!r} for level {v}"
                )
        return v
