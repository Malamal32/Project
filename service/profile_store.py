"""Persist a student-reviewed AcademicProfile to the database.

The only write path in this service, and the only place the wire schemas in
`service/schemas.py` meet the ORM in `models/student.py`. Parsing a transcript
writes nothing — the profile reaches this module only when the student POSTs it
back after review, which is what `review_required` on every ParseResponse means.

Fields are stored exactly as they were reviewed. No date parsing, no GPA
normalization, no inference of a missing value from a present one: the same
no-fabrication rule the extraction stages follow applies on the way into the
database too.
"""

from __future__ import annotations

import functools
import uuid
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

import structlog
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from models.db import get_session
from models.student import (
    StudentAttribute,
    StudentCourse,
    StudentCourseRecord,
    StudentProfile,
    StudentProfileRecord,
)
from service.schemas import AcademicProfile

log = structlog.get_logger()


def to_record(
    profile: AcademicProfile,
    *,
    extraction_method: str,
    model_version: Optional[str] = None,
) -> StudentProfileRecord:
    """Validation boundary: wire schema -> validated insert record."""
    return StudentProfileRecord(
        institution=profile.institution,
        degree=profile.degree,
        degree_level=profile.degree_level,
        major=profile.major,
        minor=profile.minor,
        concentration=profile.concentration,
        graduation_date=profile.graduation_date,
        expected_graduation_date=profile.expected_graduation_date,
        gpa=profile.gpa,
        extraction_method=extraction_method,
        model_version=model_version,
        courses=[
            StudentCourseRecord(
                course_code=course.course_code,
                course_name=course.course_name,
                credits=course.credits,
                grade=course.grade,
                term=course.term,
                student_approved=course.student_approved,
                position=position,
            )
            # `id` from the wire is a client-side handle for the review UI, not a
            # database key — a new one is minted here so a caller cannot choose
            # (or collide with) a stored primary key.
            for position, course in enumerate(profile.coursework)
        ],
        skills=list(profile.skills),
        honors=list(profile.honors),
        certifications=list(profile.certifications),
    )


def _attribute_rows(profile_id: uuid.UUID, record: StudentProfileRecord) -> List[StudentAttribute]:
    rows: List[StudentAttribute] = []
    groups: Iterable[Tuple[str, List[str]]] = (
        ("skill", record.skills),
        ("honor", record.honors),
        ("certification", record.certifications),
    )
    for attribute_type, values in groups:
        for position, value in enumerate(values):
            if not value:
                continue
            rows.append(
                StudentAttribute(
                    student_profile_id=profile_id,
                    attribute_type=attribute_type,
                    value=value,
                    position=position,
                )
            )
    return rows


def save_profile(
    profile: AcademicProfile,
    *,
    extraction_method: str,
    model_version: Optional[str] = None,
    session: Optional[Session] = None,
) -> Tuple[uuid.UUID, int, int]:
    """Insert a profile and its courses and attributes in one transaction.

    Returns (student_profile_id, courses_saved, attributes_saved). Follows the
    `owns_session` convention used by the pipeline stages so a caller (or a test)
    can supply its own session and control the transaction.
    """
    record = to_record(profile, extraction_method=extraction_method, model_version=model_version)

    owns_session = session is None
    session = session or get_session()
    try:
        now = datetime.now(timezone.utc)
        row = StudentProfile(
            institution=record.institution,
            degree=record.degree,
            degree_level=record.degree_level,
            major=record.major,
            minor=record.minor,
            concentration=record.concentration,
            graduation_date=record.graduation_date,
            expected_graduation_date=record.expected_graduation_date,
            gpa=record.gpa,
            extraction_method=record.extraction_method,
            model_version=record.model_version,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        # Assign the primary key before the children reference it, without
        # committing — a failure below still rolls the whole thing back.
        session.flush()

        courses = [
            StudentCourse(
                student_profile_id=row.student_profile_id,
                course_code=course.course_code,
                course_name=course.course_name,
                credits=course.credits,
                grade=course.grade,
                term=course.term,
                student_approved=course.student_approved,
                position=course.position,
            )
            for course in record.courses
        ]
        attributes = _attribute_rows(row.student_profile_id, record)
        session.add_all(courses)
        session.add_all(attributes)
        session.commit()

        log.info(
            "profile_store.saved",
            student_profile_id=str(row.student_profile_id),
            extraction_method=record.extraction_method,
            model_version=record.model_version,
            courses=len(courses),
            attributes=len(attributes),
        )
        return row.student_profile_id, len(courses), len(attributes)
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_session:
            session.close()


# --- Async front door ------------------------------------------------------
# `save_profile` above talks to SQLAlchemy, which is the right thing locally and
# impossible on Cloudflare Workers (no filesystem database, no threads). The
# service is deployed to both, so the HTTP layer calls `save_profile_async`
# instead and this module decides which backend is in play.
#
# There is deliberately no abstract base class. A backend is anything with an
# awaitable `save(record) -> (uuid, courses, attributes)`; `service/d1_store.py`
# is the only implementation and the protocol is two lines of duck typing.

_backend = None


def use_backend(backend) -> None:
    """Register a non-SQLAlchemy backend. Called once, by the Worker entrypoint.

    Left unset — the local and test case — the SQLAlchemy path below is used.
    """
    global _backend
    _backend = backend


async def save_profile_async(
    profile: AcademicProfile,
    *,
    extraction_method: str,
    model_version: Optional[str] = None,
) -> Tuple[uuid.UUID, int, int]:
    """Store a reviewed profile, whichever database this deploy is talking to."""
    if _backend is not None:
        record = to_record(
            profile, extraction_method=extraction_method, model_version=model_version
        )
        return await _backend.save(record)

    # `save_profile` is looked up on the module at call time rather than bound
    # at import, so a test that monkeypatches it still intercepts this path.
    return await run_in_threadpool(
        functools.partial(
            save_profile,
            profile,
            extraction_method=extraction_method,
            model_version=model_version,
        )
    )
