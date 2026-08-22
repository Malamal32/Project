"""POST /api/linkedin/import over the real HTTP surface.

The endpoint's contract is the same one /api/transcript/parse has — it reads and
returns, and it must not write. What is different, and what the last test here
is really about, is that this endpoint receives data the student did not author:
an archive member we do not read is a member we do not return.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from service.app import app
from service.config import MAX_LINKEDIN_FILES, MAX_LINKEDIN_IMPORT_CHARS

POSITIONS = (
    "Company Name,Title,Description,Location,Started On,Finished On\n"
    "Riverbend Analytics,Data Analyst Intern,Built dashboards.,Austin TX,Jun 2025,Aug 2025\n"
)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_import_returns_reviewable_experience(client):
    response = client.post("/api/linkedin/import", json={"files": {"Positions.csv": POSITIONS}})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["review_required"] is True
    assert body["files_read"] == ["Positions.csv"]
    assert body["experience"][0]["title"] == "Data Analyst Intern"
    assert body["experience"][0]["started_on"] == "Jun 2025"


def test_import_records_have_no_ids(client):
    """The browser owns the id space for experience and projects — see the note
    above ImportedExperience in service/schemas.py. A server-assigned id here
    would be a second evidence-id scheme to keep in step with the first."""
    body = client.post("/api/linkedin/import", json={"files": {"Positions.csv": POSITIONS}}).json()
    assert "id" not in body["experience"][0]


def test_import_writes_nothing(client):
    """Same guarantee /api/transcript/parse gives. Storing anything is still the
    separate, explicit /api/student/profile call."""
    with patch("service.app.profile_store.save_profile") as save:
        client.post("/api/linkedin/import", json={"files": {"Positions.csv": POSITIONS}})
    save.assert_not_called()


def test_import_calls_no_model(client):
    """There is no extraction stage here and there should never be one. CSV
    columns are already structured; a model in front of them could only
    introduce a reading that differs from what the student wrote."""
    with patch("service.llm_extraction.extract_academic_profile") as extract:
        client.post("/api/linkedin/import", json={"files": {"Positions.csv": POSITIONS}})
    extract.assert_not_called()


def test_an_empty_request_is_answered_not_rejected(client):
    response = client.post("/api/linkedin/import", json={"files": {}})
    assert response.status_code == 200
    body = response.json()
    assert body["experience"] == []
    assert any("didn't find any of the files" in w for w in body["warnings"])


def test_too_many_files_is_refused(client):
    files = {f"Positions{i}.csv": POSITIONS for i in range(MAX_LINKEDIN_FILES + 1)}
    assert client.post("/api/linkedin/import", json={"files": files}).status_code == 413


def test_an_oversized_import_is_refused(client):
    files = {"Positions.csv": "x" * (MAX_LINKEDIN_IMPORT_CHARS + 1)}
    assert client.post("/api/linkedin/import", json={"files": files}).status_code == 413


def test_a_member_outside_the_whitelist_is_reported_and_not_echoed(client):
    """The browser should never send this. If it ever does, the response must
    not become a way to read someone's connections back out of the service."""
    contacts = "First Name,Last Name,Email Address\nSam,Okafor,sam@example.com\n"
    response = client.post(
        "/api/linkedin/import", json={"files": {"Positions.csv": POSITIONS, "Connections.csv": contacts}}
    )

    body = response.json()
    assert [f["name"] for f in body["files_ignored"]] == ["Connections.csv"]
    assert "Okafor" not in response.text
    assert "sam@example.com" not in response.text
    # The rest of the archive still imports.
    assert len(body["experience"]) == 1
