"""Persisting a student-reviewed profile.

Runs against the `db_session` fixture, which builds its schema from
`migrations/d1/*.sql` — the same DDL that ships to D1 — so these exercise the
real CHECK constraints and foreign keys.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models.student import StudentAttribute, StudentCourse, StudentProfile
from service.profile_store import save_profile, to_record
from service.schemas import AcademicProfile, Coursework


def _profile() -> AcademicProfile:
    return AcademicProfile(
        institution="Riverbend State University",
        degree="Bachelor of Science",
        degree_level="Bachelor's",
        major="Computer Science",
        expected_graduation_date="May 2027",
        gpa="3.72/4.00",
        coursework=[
            Coursework(id="client-1", course_code="CS 310", course_name="Data Structures", credits=3, grade="A",
                       term="Fall 2025", student_approved=True),
            Coursework(id="client-2", course_code="CS 340", course_name="Database Systems", credits=3, grade="A-"),
        ],
        skills=["Python", "SQL"],
        honors=["Dean's List, Fall 2025"],
        certifications=["AWS Cloud Practitioner"],
    )


def test_round_trip(db_session):
    profile_id, courses, attributes = save_profile(
        _profile(), extraction_method="llm", model_version="claude-opus-5", session=db_session
    )

    assert courses == 2
    assert attributes == 4

    row = db_session.get(StudentProfile, profile_id)
    assert row.institution == "Riverbend State University"
    assert row.extraction_method == "llm"
    assert row.model_version == "claude-opus-5"
    assert row.created_at.tzinfo is not None  # TZDateTime reattaches UTC


def test_values_are_stored_verbatim(db_session):
    """No date parsing, no GPA normalization — the same no-fabrication rule the
    extraction stages follow applies on the way into the database."""
    profile_id, _, _ = save_profile(_profile(), extraction_method="rules", session=db_session)

    row = db_session.get(StudentProfile, profile_id)
    assert row.expected_graduation_date == "May 2027"
    assert row.gpa == "3.72/4.00"
    assert row.model_version is None


def test_course_order_is_preserved(db_session):
    profile_id, _, _ = save_profile(_profile(), extraction_method="manual", session=db_session)

    rows = db_session.scalars(
        select(StudentCourse).where(StudentCourse.student_profile_id == profile_id).order_by(StudentCourse.position)
    ).all()
    assert [r.course_code for r in rows] == ["CS 310", "CS 340"]
    assert [r.position for r in rows] == [0, 1]


def test_approval_flags_survive_unchanged(db_session):
    profile_id, _, _ = save_profile(_profile(), extraction_method="manual", session=db_session)

    rows = db_session.scalars(
        select(StudentCourse).where(StudentCourse.student_profile_id == profile_id).order_by(StudentCourse.position)
    ).all()
    assert [r.student_approved for r in rows] == [True, False]


def test_client_supplied_course_ids_are_not_used_as_primary_keys(db_session):
    """`id` on the wire is a handle for the review UI. A caller must not be able
    to choose — or collide with — a stored primary key."""
    profile_id, _, _ = save_profile(_profile(), extraction_method="manual", session=db_session)

    rows = db_session.scalars(
        select(StudentCourse).where(StudentCourse.student_profile_id == profile_id)
    ).all()
    assert all(isinstance(r.student_course_id, uuid.UUID) for r in rows)
    assert {str(r.student_course_id) for r in rows}.isdisjoint({"client-1", "client-2"})


def test_attributes_are_split_by_type(db_session):
    profile_id, _, _ = save_profile(_profile(), extraction_method="llm", session=db_session)

    rows = db_session.scalars(
        select(StudentAttribute).where(StudentAttribute.student_profile_id == profile_id)
    ).all()
    by_type = {}
    for row in rows:
        by_type.setdefault(row.attribute_type, []).append(row.value)

    assert by_type["skill"] == ["Python", "SQL"]
    assert by_type["honor"] == ["Dean's List, Fall 2025"]
    assert by_type["certification"] == ["AWS Cloud Practitioner"]


def test_empty_profile_saves_with_no_children(db_session):
    profile_id, courses, attributes = save_profile(
        AcademicProfile(), extraction_method="manual", session=db_session
    )
    assert (courses, attributes) == (0, 0)
    assert db_session.get(StudentProfile, profile_id) is not None


def test_unknown_extraction_method_is_rejected_before_the_database(db_session):
    with pytest.raises(Exception):  # pydantic ValidationError from StudentProfileRecord
        to_record(_profile(), extraction_method="guessed")


def test_attribute_type_check_constraint_is_enforced(db_session):
    """The CHECK reaches D1 too, so it is worth proving it is really there."""
    profile_id, _, _ = save_profile(_profile(), extraction_method="manual", session=db_session)

    db_session.add(
        StudentAttribute(
            student_profile_id=profile_id, attribute_type="hobby", value="chess", position=0
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_orphan_course_is_rejected_by_the_foreign_key(db_session):
    db_session.add(
        StudentCourse(student_profile_id=uuid.uuid4(), course_name="Nowhere 101", position=0)
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
