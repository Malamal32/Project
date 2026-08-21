"""The resume HTTP surface. Starlette's TestClient — no server, no network, and
the generation stage patched out so nothing reaches the Claude API.

The contract these tests hold: the endpoint never 500s, never persists, and
never returns a claim the evidence contract rejected.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from models.student import StudentProfile
from service import resume_generation
from service.app import app
from service.config import RESUME_MAX_VARIANT
from service.schemas import (
    Claim,
    EducationBlock,
    ExperienceEntry,
    ResumeDocument,
    SkillEntry,
)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


PAYLOAD = {
    "career": {"role": "Backend Engineer", "level": "Entry level"},
    "academic_profile": {
        "institution": "Riverbend State University",
        "degree": "Bachelor of Science",
        "major": "Computer Science",
        "expected_graduation_date": "May 2027",
        "coursework": [{"id": "c1", "course_code": "CS 310", "course_name": "Data Structures"}],
        "skills": ["Python"],
    },
    "experience": [{"id": "exp_1", "description": "Answered student support tickets."}],
    "projects": [{"id": "proj_1", "technologies": "Python", "description": "Built a course planner."}],
    "market_profile": {
        "skills": [
            {"name": "Python", "posting_count": 800, "frequency": 0.8},
            {"name": "Kubernetes", "posting_count": 600, "frequency": 0.6},
        ]
    },
    "variant": 0,
}

DOCUMENT = ResumeDocument(
    summary=Claim(text="Computer Science student seeking a backend role.", evidence=["edu_major"]),
    skills=[SkillEntry(name="Python", evidence=["skill_0"])],
    experience=[
        ExperienceEntry(
            experience_id="exp_1",
            bullets=[Claim(text="Answered student support tickets.", evidence=["exp_1"])],
        )
    ],
    education=EducationBlock(institution="Riverbend State University"),
)


def _generating(document=DOCUMENT, dropped=(), warnings=()):
    """Patch the stage as enabled and returning a canned document."""
    return (
        patch.object(resume_generation, "is_enabled", return_value=True),
        patch.object(
            resume_generation,
            "generate_resume",
            return_value=(document, list(dropped), list(warnings)),
        ),
    )


def test_generates_a_resume(client):
    enabled, generating = _generating()
    with enabled, generating:
        response = client.post("/api/resume/generate", json=PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["summary"] == "Computer Science student seeking a backend role."
    assert body["resume"]["skills"][0]["name"] == "Python"
    assert body["model_version"]


def test_summary_is_mirrored_flat_for_the_existing_browser_caller(client):
    """frontend/js/services/api-service.js destructures `{ summary }` and nothing
    else. Keeping that key top-level is what makes swapping the mock for a
    fetch() a one-line change."""
    enabled, generating = _generating()
    with enabled, generating:
        body = client.post("/api/resume/generate", json=PAYLOAD).json()

    assert isinstance(body["summary"], str) and body["summary"]


def test_gaps_are_reported_and_never_folded_into_the_resume(client):
    """Kubernetes is in demand and absent from the profile. It belongs in gaps,
    not in the resume."""
    enabled, generating = _generating()
    with enabled, generating:
        body = client.post("/api/resume/generate", json=PAYLOAD).json()

    assert "Kubernetes" in body["gaps"]
    assert "Kubernetes" not in [s["name"] for s in body["resume"]["skills"]]


def test_match_is_computed_server_side_from_the_submitted_profile(client):
    """The request carries no MarketMatch — the browser's ids don't resolve here,
    so the server always recomputes it."""
    assert "market_match" not in PAYLOAD

    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return DOCUMENT, [], []

    with patch.object(resume_generation, "is_enabled", return_value=True), patch.object(
        resume_generation, "generate_resume", side_effect=capture
    ):
        client.post("/api/resume/generate", json=PAYLOAD)

    statuses = {m.market_skill: m.status for m in captured["market_match"].matches}
    assert statuses["Python"] == "verified"
    assert statuses["Kubernetes"] == "not_verified"


def test_dropped_claims_are_surfaced_to_the_student(client):
    from service.schemas import DroppedClaim

    dropped = [
        DroppedClaim(section="experience", text="Led a team of 5.", evidence=["exp_1"], reason="unsupported_number")
    ]
    enabled, generating = _generating(dropped=dropped, warnings=["1 generated line(s) were removed"])
    with enabled, generating:
        body = client.post("/api/resume/generate", json=PAYLOAD).json()

    assert body["dropped"][0]["reason"] == "unsupported_number"
    assert body["warnings"]


def test_generation_failure_is_a_200_with_a_warning_not_a_500(client):
    """A career tool that 500s mid-flow loses the experience the student just
    typed in. Failures come back as success=False."""
    with patch.object(resume_generation, "is_enabled", return_value=True), patch.object(
        resume_generation,
        "generate_resume",
        side_effect=resume_generation.ResumeGenerationError("API error 429 (rate_limit_error)"),
    ):
        response = client.post("/api/resume/generate", json=PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["warnings"]
    assert body["resume"] is None


def test_failure_response_does_not_leak_the_internal_reason(client):
    with patch.object(resume_generation, "is_enabled", return_value=True), patch.object(
        resume_generation,
        "generate_resume",
        side_effect=resume_generation.ResumeGenerationError("API error 429 (rate_limit_error)"),
    ):
        body = client.post("/api/resume/generate", json=PAYLOAD).json()

    assert not any("429" in w for w in body["warnings"])


def test_disabled_stage_returns_a_warning_and_still_reports_gaps(client):
    """No credentials in the test environment, so the stage is off by default —
    the student still learns which skills they're missing."""
    response = client.post("/api/resume/generate", json=PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["warnings"]
    assert "Kubernetes" in body["gaps"]


def test_variant_is_bounded(client):
    """A client cannot push an arbitrary integer into the prompt."""
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return DOCUMENT, [], []

    with patch.object(resume_generation, "is_enabled", return_value=True), patch.object(
        resume_generation, "generate_resume", side_effect=capture
    ):
        body = client.post("/api/resume/generate", json={**PAYLOAD, "variant": 10_000}).json()

    assert captured["variant"] == RESUME_MAX_VARIANT
    assert body["variant"] == RESUME_MAX_VARIANT


def test_negative_variant_is_clamped(client):
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return DOCUMENT, [], []

    with patch.object(resume_generation, "is_enabled", return_value=True), patch.object(
        resume_generation, "generate_resume", side_effect=capture
    ):
        client.post("/api/resume/generate", json={**PAYLOAD, "variant": -5})

    assert captured["variant"] == 0


def test_generate_persists_nothing(client, db_session):
    enabled, generating = _generating()
    with enabled, generating:
        client.post("/api/resume/generate", json=PAYLOAD)

    assert db_session.execute(select(func.count()).select_from(StudentProfile)).scalar_one() == 0


def test_malformed_request_is_a_422(client):
    response = client.post("/api/resume/generate", json={"career": {}})
    assert response.status_code == 422
