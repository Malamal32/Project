"""Student academic profiles — the demand side's counterpart to `postings`.

These rows are what the transcript service writes *after* a student has reviewed
what was extracted. The transcript PDF and its extracted text never land here:
only the structured fields the student approved. See `service/transcript_service.py`
and README -> Transcript extraction service -> Data handling.

Values are stored **verbatim as extracted** — "May 2027" stays "May 2027",
"3.72/4.00" keeps its scale. Normalizing on the way in would discard what the
document actually said, which is the same reason `postings.title_raw` and
`companies.canonical_name` keep their source spelling. `major` in particular is
free text on purpose: mapping it to a CIP code belongs with the `major_alias`
work in Phase 6, alongside the identical problem on the posting side, not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base
from models.types import TZDateTime

# Which stage produced the fields. "manual" is legitimate and common — a student
# may correct or type in everything themselves.
EXTRACTION_METHODS = ("llm", "rules", "manual")

ATTRIBUTE_TYPES = ("skill", "honor", "certification")


class StudentProfile(Base):
    __tablename__ = "student_profiles"
    __table_args__ = (
        CheckConstraint(f"extraction_method IN {EXTRACTION_METHODS}", name="ck_student_profiles_extraction_method"),
    )

    student_profile_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    institution: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    degree: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    degree_level: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    major: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    minor: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    concentration: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    graduation_date: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    expected_graduation_date: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    gpa: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    extraction_method: Mapped[str] = mapped_column(String(20), nullable=False)
    # Which model produced the fields, when extraction_method is 'llm'. Null for
    # the rule-based and manual paths — the same provenance question every
    # derived fact in this database has to answer.
    model_version: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class StudentCourse(Base):
    """One course from a student's transcript.

    `position` preserves document order, which the extraction rules require the
    model to respect and which a plain ORDER BY on any other column would lose.
    """

    __tablename__ = "student_courses"
    __table_args__ = (Index("ix_student_courses_profile_id", "student_profile_id"),)

    student_course_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("student_profiles.student_profile_id"), nullable=False
    )
    course_code: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    course_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    credits: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    grade: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    term: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    student_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class StudentAttribute(Base):
    """A skill, honor, or certification the student listed.

    A child table rather than a JSON column because these are the rows Phase 5's
    `posting_requirements` will be joined against — `sources.politeness_config`
    is the only JSON column here, and that is config, not queried data.
    """

    __tablename__ = "student_attributes"
    __table_args__ = (
        CheckConstraint(f"attribute_type IN {ATTRIBUTE_TYPES}", name="ck_student_attributes_attribute_type"),
        Index("ix_student_attributes_profile_id", "student_profile_id"),
        Index("ix_student_attributes_type", "attribute_type"),
    )

    student_attribute_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("student_profiles.student_profile_id"), nullable=False
    )
    attribute_type: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[str] = mapped_column(String(300), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class StudentCourseRecord(BaseModel):
    """Validation boundary for one course row before it is inserted."""

    model_config = ConfigDict(str_strip_whitespace=True)

    course_code: Optional[str] = None
    course_name: Optional[str] = None
    credits: Optional[float] = None
    grade: Optional[str] = None
    term: Optional[str] = None
    student_approved: bool = False
    position: int


class StudentProfileRecord(BaseModel):
    """Validation boundary for a student profile and its children before insert."""

    model_config = ConfigDict(str_strip_whitespace=True)

    institution: Optional[str] = None
    degree: Optional[str] = None
    degree_level: Optional[str] = None
    major: Optional[str] = None
    minor: Optional[str] = None
    concentration: Optional[str] = None
    graduation_date: Optional[str] = None
    expected_graduation_date: Optional[str] = None
    gpa: Optional[str] = None
    extraction_method: Literal["llm", "rules", "manual"]
    model_version: Optional[str] = None
    courses: List[StudentCourseRecord] = []
    skills: List[str] = []
    honors: List[str] = []
    certifications: List[str] = []
