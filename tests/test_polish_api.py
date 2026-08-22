"""The description-polish HTTP surface. Starlette's TestClient — no server, no
network, and the stage patched out so nothing reaches the Claude API.

The contract these tests hold: the endpoint never 500s, never persists, and
never returns a `description` the browser could assign over the student's own
text unless the polish actually succeeded.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from models.student import StudentProfile
from service import description_polish
from service.app import app
from service.config import MAX_POLISH_INPUT_CHARS


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


EXPERIENCE_PAYLOAD = {
    "kind": "experience",
    "experience": {
        "id": "exp_1",
        "title": "IT Help Desk Assistant",
        "organization": "Riverbend State University",
        "dates": "Jun 2024 – Aug 2024",
        "description": "worked the help desk, wrote a script in Python to close stale tickets",
    },
}

PROJECT_PAYLOAD = {
    "kind": "project",
    "project": {
        "id": "proj_1",
        "name": "Trailhead",
        "technologies": "React, Postgres",
        "description": "built a trail-finding app for a class project",
    },
}


def _stage(description="Resolved help desk tickets", warnings=None):
    async def fake(*, kind, item):
        return description, warnings or []

    return fake


# --- happy path ------------------------------------------------------------


def test_polishes_an_experience(client):
    with patch.object(description_polish, "is_enabled", return_value=True), patch.object(
        description_polish, "polish_description", _stage()
    ):
        response = client.post("/api/description/polish", json=EXPERIENCE_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["description"] == "Resolved help desk tickets"
    assert body["model_version"]


def test_polishes_a_project(client):
    with patch.object(description_polish, "is_enabled", return_value=True), patch.object(
        description_polish, "polish_description", _stage("Built a trail-finding app")
    ):
        response = client.post("/api/description/polish", json=PROJECT_PAYLOAD)

    assert response.json()["description"] == "Built a trail-finding app"


def test_guard_warnings_reach_the_student(client):
    """A partially-guarded result is still a success — the student gets the
    surviving lines and is told what was removed."""
    with patch.object(description_polish, "is_enabled", return_value=True), patch.object(
        description_polish,
        "polish_description",
        _stage("Resolved help desk tickets", ["1 drafted line(s) were removed"]),
    ):
        body = client.post("/api/description/polish", json=EXPERIENCE_PAYLOAD).json()

    assert body["success"] is True
    assert body["warnings"] == ["1 drafted line(s) were removed"]


# --- degraded paths --------------------------------------------------------


def test_disabled_stage_returns_an_empty_description(client):
    """The test that guarantees this endpoint cannot blank what the student
    typed. The browser assigns only when `success` and `description` are both
    truthy, so `description == ""` here is the whole no-fallback contract."""
    with patch.object(description_polish, "is_enabled", return_value=False):
        response = client.post("/api/description/polish", json=EXPERIENCE_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["description"] == ""
    assert body["warnings"] and "unchanged" in body["warnings"][0]


def test_stage_failure_is_a_200_with_a_generic_warning(client):
    """Never a 500: a career tool that crashes mid-flow loses the student's
    typed-in experience along with it."""

    async def fail(*, kind, item):
        raise description_polish.DescriptionPolishError("API error 429 (rate_limit_error)")

    with patch.object(description_polish, "is_enabled", return_value=True), patch.object(
        description_polish, "polish_description", fail
    ):
        response = client.post("/api/description/polish", json=EXPERIENCE_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["description"] == ""
    # The internal reason goes to the log, not to the student.
    assert "429" not in " ".join(body["warnings"])


def test_rejection_message_reaches_the_student_verbatim(client):
    """`DescriptionPolishRejected` carries a message written here, about the
    student's own text, and is the one exception whose string is shown."""
    message = "The rewrite left out Python — those are worth keeping."

    async def reject(*, kind, item):
        raise description_polish.DescriptionPolishRejected(message)

    with patch.object(description_polish, "is_enabled", return_value=True), patch.object(
        description_polish, "polish_description", reject
    ):
        body = client.post("/api/description/polish", json=EXPERIENCE_PAYLOAD).json()

    assert body["success"] is False
    assert body["warnings"] == [message]


def test_oversize_description_is_refused_without_calling_the_model(client):
    """Checked before the enablement gate so the student gets the advice that
    fixes it rather than "temporarily unavailable"."""
    called = False

    async def fake(*, kind, item):
        nonlocal called
        called = True
        return "", []

    payload = {
        "kind": "experience",
        "experience": {
            **EXPERIENCE_PAYLOAD["experience"],
            "description": "x" * (MAX_POLISH_INPUT_CHARS + 1),
        },
    }
    with patch.object(description_polish, "is_enabled", return_value=True), patch.object(
        description_polish, "polish_description", fake
    ):
        response = client.post("/api/description/polish", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "trim it" in body["warnings"][0]
    assert called is False


# --- request validation ----------------------------------------------------


def test_kind_without_a_matching_item_is_a_422(client):
    response = client.post(
        "/api/description/polish",
        json={"kind": "experience", "project": PROJECT_PAYLOAD["project"]},
    )
    assert response.status_code == 422


def test_empty_body_is_a_422(client):
    assert client.post("/api/description/polish", json={}).status_code == 422


# --- persistence -----------------------------------------------------------


def test_polish_persists_nothing(client, db_session):
    with patch.object(description_polish, "is_enabled", return_value=True), patch.object(
        description_polish, "polish_description", _stage()
    ):
        client.post("/api/description/polish", json=EXPERIENCE_PAYLOAD)

    assert db_session.execute(select(func.count()).select_from(StudentProfile)).scalar_one() == 0
