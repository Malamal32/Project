from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class Occupation(Base):
    __tablename__ = "occupations"

    onet_soc_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    soc_2018_code: Mapped[str] = mapped_column(String(8), nullable=False)
    job_zone: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bright_outlook: Mapped[Optional[bool]] = mapped_column(nullable=True)


class OccupationAltTitle(Base):
    __tablename__ = "occupation_alt_titles"

    onet_soc_code: Mapped[str] = mapped_column(
        String(10), ForeignKey("occupations.onet_soc_code"), primary_key=True
    )
    alt_title: Mapped[str] = mapped_column(String(300), primary_key=True)
    short_title: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)


class CipSocCrosswalk(Base):
    __tablename__ = "cip_soc_crosswalk"

    cip_code: Mapped[str] = mapped_column(
        String(9), ForeignKey("cip_codes.cip_code"), primary_key=True
    )
    onet_soc_code: Mapped[str] = mapped_column(
        String(10), ForeignKey("occupations.onet_soc_code"), primary_key=True
    )
    crosswalk_source: Mapped[str] = mapped_column(String(50), primary_key=True)
    retrieved_at: Mapped[str] = mapped_column(String(30), nullable=False)


class OccupationRecord(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    onet_soc_code: str
    title: str
    description: Optional[str] = None
    soc_2018_code: str
    job_zone: Optional[int] = None
    bright_outlook: Optional[bool] = None


class OccupationAltTitleRecord(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    onet_soc_code: str
    alt_title: str
    short_title: Optional[str] = None
    source: Optional[str] = None


class CipSocCrosswalkRecord(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    cip_code: str
    onet_soc_code: str
    crosswalk_source: str
    retrieved_at: str
