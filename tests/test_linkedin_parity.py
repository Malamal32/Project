"""The browser and the server must agree on which archive members get opened.

`frontend/js/services/linkedin-import.js` decides what leaves the student's
machine; `service/linkedin_import.py` decides what gets parsed on arrival. The
two lists are deliberately duplicated — one filename list standing between a
LinkedIn archive's messages and an HTTP request is a single point of failure —
but duplicated is only useful while they agree. If the browser's list grows a
name the server does not read, the browser starts uploading data nothing wanted.

The zip reader is tested here too rather than in isolation, because it is
hand-rolled: there is no zip library in `frontend/vendor`, so the container
parsing is ours and a mistake in it reads the wrong bytes and blames the CSV.
The fixture archive is built by Python's `zipfile`, which is to say by an
implementation that had no part in writing the reader.

Skipped when node is unavailable (the devcontainer installs Python only).
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from service.linkedin_import import EXPORT_FILES, parse_export

MODULE = Path(__file__).resolve().parent.parent / "frontend" / "js" / "services" / "linkedin-import.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; frontend parity not checked"
)

POSITIONS = (
    "Company Name,Title,Description,Location,Started On,Finished On\n"
    "Riverbend Analytics,Data Analyst Intern,Built dashboards.,Austin TX,Jun 2025,Aug 2025\n"
)
SKILLS = "Name\nPython\nSQL\n"
# The members that make the whitelist load-bearing: other people's names and
# addresses, sitting in the same archive as the two files above.
CONNECTIONS = "First Name,Last Name,Email Address,Company\nSam,Okafor,sam@example.com,Acme\n"
MESSAGES = "CONVERSATION ID,FROM,TO,CONTENT\n1,Sam Okafor,Jordan,See you Thursday\n"


def _archive() -> bytes:
    """A stand-in for the real thing: nested under an export folder the way
    LinkedIn ships it, one member stored rather than deflated, and carrying the
    two files the reader must not open."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        root = "Basic_LinkedInDataExport_2026-08-21/"
        archive.writestr(root + "Positions.csv", POSITIONS)
        # Stored, not deflated — the reader has to handle both, and a tiny file
        # is exactly the case a real zip writer leaves uncompressed.
        archive.writestr(zipfile.ZipInfo(root + "Skills.csv"), SKILLS, compress_type=zipfile.ZIP_STORED)
        archive.writestr(root + "Connections.csv", CONNECTIONS)
        archive.writestr(root + "messages.csv", MESSAGES)
        archive.writestr(root + "Rich_Media.csv", "Type,Url\nIMAGE,https://example.com/a.png\n")
    return buffer.getvalue()


def _run_in_node(body: str) -> dict:
    script = f"""
    import('{MODULE.as_posix()}').then(async (m) => {{
      {body}
    }}).catch(e => {{ console.error(e); process.exit(1); }});
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return json.loads(result.stdout)


def _read_archive_in_node(payload: bytes, filename: str = "export.zip") -> dict:
    encoded = base64.b64encode(payload).decode()
    return _run_in_node(
        f"""
        const bytes = Uint8Array.from(atob("{encoded}"), c => c.charCodeAt(0));
        const file = new File([bytes], "{filename}");
        try {{
          const out = await m.readExport(file);
          process.stdout.write(JSON.stringify({{ ok: true, ...out }}));
        }} catch (err) {{
          process.stdout.write(JSON.stringify({{ ok: false, message: err.message }}));
        }}
        """
    )


def test_the_two_whitelists_are_the_same_list():
    browser = _run_in_node('process.stdout.write(JSON.stringify([...m.WANTED_FILES]));')
    assert sorted(browser) == sorted(EXPORT_FILES)


def test_the_browser_extracts_only_the_whitelisted_members():
    result = _read_archive_in_node(_archive())

    assert result["ok"] is True
    assert sorted(result["files"]) == ["positions.csv", "skills.csv"]
    # Not "absent from the output" — never decompressed at all. The assertion
    # that matters is that no part of them is in the payload the browser holds.
    blob = json.dumps(result)
    assert "Okafor" not in blob
    assert "sam@example.com" not in blob
    assert "See you Thursday" not in blob
    assert result["skipped"] == 3


def test_deflated_and_stored_members_both_come_back_intact():
    result = _read_archive_in_node(_archive())
    assert result["files"]["positions.csv"] == POSITIONS   # deflated
    assert result["files"]["skills.csv"] == SKILLS         # stored


def test_what_the_browser_extracts_is_what_the_server_can_parse():
    """The end-to-end shape of the feature: reader output feeds the mapper
    directly, so a change to either that breaks the join fails here."""
    extracted = _read_archive_in_node(_archive())["files"]
    imported = parse_export(extracted)

    assert imported.files_read == ["Positions.csv", "Skills.csv"]
    assert imported.files_ignored == []
    assert imported.experience[0].title == "Data Analyst Intern"
    assert imported.experience[0].organization == "Riverbend Analytics"
    assert imported.skills == ["Python", "SQL"]


def test_a_loose_csv_is_accepted_and_an_unwanted_one_is_not():
    positions = _run_in_node(
        f"""
        const file = new File([{json.dumps(POSITIONS)}], "Positions.csv");
        const out = await m.readExport(file);
        process.stdout.write(JSON.stringify(out));
        """
    )
    assert positions["files"] == {"positions.csv": POSITIONS}

    connections = _run_in_node(
        f"""
        const file = new File([{json.dumps(CONNECTIONS)}], "Connections.csv");
        try {{
          await m.readExport(file);
          process.stdout.write(JSON.stringify({{ ok: true }}));
        }} catch (err) {{
          process.stdout.write(JSON.stringify({{ ok: false, message: err.message }}));
        }}
        """
    )
    assert connections["ok"] is False
    assert "not one this importer reads" in connections["message"]


def test_a_file_that_is_not_an_archive_fails_with_something_a_student_can_act_on():
    result = _read_archive_in_node(b"%PDF-1.7 not a zip at all", filename="profile.pdf")
    assert result["ok"] is False
    assert result["message"] == "Please pick the .zip archive LinkedIn sent you, or a single .csv from it."

    truncated = _read_archive_in_node(b"PK\x03\x04" + b"\x00" * 64)
    assert truncated["ok"] is False
    assert "zip" in truncated["message"].lower()
