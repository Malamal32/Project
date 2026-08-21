"""The HTTP surface. Uses Starlette's TestClient — no server, no network.

The endpoint split is the privacy boundary these tests are really about:
/api/transcript/parse must never write, and /api/student/profile is the only
thing that does.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from models.student import StudentCourse, StudentProfile
from service import profile_store
from service.app import app
from service.config import MAX_UPLOAD_BYTES


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_writing_to(db_session, monkeypatch):
    """A client whose save path writes to the test's throwaway database instead
    of the real one configured in models.db."""
    real_save = profile_store.save_profile

    def save_into_test_db(profile, **kwargs):
        kwargs.pop("session", None)
        return real_save(profile, session=db_session, **kwargs)

    monkeypatch.setattr("service.app.profile_store.save_profile", save_into_test_db)
    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_parse_returns_a_reviewable_profile(client, synthetic_transcript_pdf):
    response = client.post(
        "/api/transcript/parse",
        files={"file": ("transcript.pdf", synthetic_transcript_pdf, "application/pdf")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["review_required"] is True
    assert body["extraction_method"] == "rules"  # no credentials in tests
    assert body["academic_profile"]["institution"] == "Riverbend State University"


def test_parse_persists_nothing(client, synthetic_transcript_pdf, db_session):
    client.post(
        "/api/transcript/parse",
        files={"file": ("transcript.pdf", synthetic_transcript_pdf, "application/pdf")},
    )

    assert db_session.execute(select(func.count()).select_from(StudentProfile)).scalar_one() == 0


def test_oversized_upload_is_rejected_before_validation(client):
    """The read loop caps bytes so an oversized upload can't exhaust memory
    before our own size check ever runs."""
    oversized = b"%PDF-" + b"0" * (MAX_UPLOAD_BYTES + 1024)
    response = client.post(
        "/api/transcript/parse",
        files={"file": ("big.pdf", oversized, "application/pdf")},
    )
    assert response.status_code == 413


def test_non_pdf_gets_a_student_facing_warning_not_a_500(client):
    response = client.post(
        "/api/transcript/parse",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["warnings"]
    assert body["review_required"] is True


def test_save_profile_writes_the_reviewed_fields(client_writing_to, db_session):
    payload = {
        "academic_profile": {
            "institution": "Riverbend State University",
            "major": "Computer Science",
            "gpa": "3.72/4.00",
            "coursework": [
                {"id": "c1", "course_code": "CS 310", "course_name": "Data Structures", "student_approved": True}
            ],
            "skills": ["Python"],
        },
        "extraction_method": "manual",
    }
    response = client_writing_to.post("/api/student/profile", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["courses_saved"] == 1
    assert body["attributes_saved"] == 1

    row = db_session.get(StudentProfile, __import__("uuid").UUID(body["student_profile_id"]))
    assert row.major == "Computer Science"
    assert row.model_version is None  # manual path has no model behind it

    course = db_session.scalars(select(StudentCourse)).one()
    assert course.course_code == "CS 310"


def test_model_version_is_recorded_only_for_the_llm_path(client_writing_to, db_session):
    response = client_writing_to.post(
        "/api/student/profile",
        json={"academic_profile": {"major": "Biology"}, "extraction_method": "llm"},
    )

    row = db_session.get(StudentProfile, __import__("uuid").UUID(response.json()["student_profile_id"]))
    assert row.model_version is not None


def test_unknown_extraction_method_is_a_422(client):
    response = client.post(
        "/api/student/profile",
        json={"academic_profile": {"major": "Biology"}, "extraction_method": "vibes"},
    )
    assert response.status_code == 422


def test_llm_path_is_reported_when_the_api_stage_succeeds(client, synthetic_transcript_pdf):
    from service import llm_extraction
    from service.schemas import AcademicProfile

    profile = AcademicProfile(institution="From The Model University")
    with patch.object(llm_extraction, "is_enabled", return_value=True), patch.object(
        llm_extraction, "extract_academic_profile", return_value=(profile, [])
    ):
        response = client.post(
            "/api/transcript/parse",
            files={"file": ("t.pdf", synthetic_transcript_pdf, "application/pdf")},
        )

    body = response.json()
    assert body["extraction_method"] == "llm"
    assert body["academic_profile"]["institution"] == "From The Model University"
