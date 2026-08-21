"""The browser and the server must agree on what `skill_0` means.

`service/resume_evidence.py` mints evidence ids by position in the profile's
`skills` / `certifications` / `honors` lists, and takes coursework ids straight
off the request. The browser builds that request. So the ordering the browser
uses when it folds the wizard's two sources (`academic` + `activities`) into one
profile *is* the id scheme, and if the two sides ever disagree the validator
drops every skill claim as `unknown_evidence_id` — silently, and completely.

This test runs the real browser code rather than reimplementing it, because a
Python copy of the JS ordering would be a third thing to keep in step and would
drift in exactly the same way it is meant to catch.

Skipped when node is unavailable (the devcontainer installs Python only).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from service.resume_evidence import collect_evidence_ids
from service.schemas import AcademicProfile

API_SERVICE = Path(__file__).resolve().parent.parent / "frontend" / "js" / "services" / "api-service.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; frontend parity not checked"
)


# Deliberately populates both sources for skills and certifications: a profile
# that only fills one of them passes no matter which order the browser uses.
ACADEMIC = {
    "institution": "Riverbend State University",
    "degree": "Bachelor of Science",
    "degreeLevel": "Bachelor",
    "major": "Computer Science",
    "gradDate": "May 2027",
    "gpa": "3.72/4.00",
    "coursework": ["CS 3305 — Data Structures", "Statistics I"],
    "skills": ["Python", "SQL"],
    "certifications": ["AWS Cloud Practitioner"],
    "honors": ["Dean's List, Fall 2025"],
}
ACTIVITIES = {"skills": ["Git"], "certifications": ["CPR"]}


def _browser_wire_profile() -> dict:
    """The AcademicProfile the browser would POST for the fixture above."""
    script = f"""
    import('{API_SERVICE.as_posix()}').then(m => {{
      process.stdout.write(JSON.stringify(m.toWireProfile(
        {json.dumps(ACADEMIC)}, {json.dumps(ACTIVITIES)}
      )));
    }});
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return json.loads(result.stdout)


def _browser_match_ids() -> dict:
    """The ids the browser's display-only matcher assigns: id -> name."""
    script = f"""
    import('{API_SERVICE.as_posix()}').then(m => {{
      const p = m.toMatchProfile({json.dumps(ACADEMIC)}, {json.dumps(ACTIVITIES)}, [], []);
      const out = {{}};
      for (const s of p.skills) out[s.id] = s.name;
      for (const c of p.certifications) out[c.id] = c.name;
      for (const c of p.coursework) out[c.id] = [c.course_code, c.course_name].filter(Boolean).join(' ');
      process.stdout.write(JSON.stringify(out));
    }});
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return json.loads(result.stdout)


def test_browser_profile_is_a_valid_academic_profile():
    """The wire shape the browser builds must validate as the request model —
    a stray camelCase key or a coursework entry without an id fails here rather
    than as a 422 the student sees."""
    profile = AcademicProfile.model_validate(_browser_wire_profile())

    assert profile.institution == "Riverbend State University"
    assert profile.major == "Computer Science"
    # `gradDate` is one UI field; the validator accepts either date field.
    assert profile.expected_graduation_date == "May 2027"
    assert [c.id for c in profile.coursework] == ["course_0", "course_1"]
    assert profile.coursework[0].course_code == "CS 3305"
    assert profile.coursework[0].course_name == "Data Structures"


def test_evidence_ids_mean_the_same_thing_on_both_sides():
    """The heart of it: resolve every id the browser assigns against the ids the
    server mints, and require the same underlying item."""
    profile = AcademicProfile.model_validate(_browser_wire_profile())
    server = collect_evidence_ids(profile)
    browser = _browser_match_ids()

    assert browser, "the browser assigned no evidence ids"

    for item_id, browser_text in browser.items():
        assert item_id in server, (
            f"the browser cites {item_id!r} but the server mints no such id — "
            "every claim citing it would be dropped as unknown_evidence_id"
        )
        assert browser_text in server[item_id].text, (
            f"{item_id!r} is {browser_text!r} in the browser but "
            f"{server[item_id].text!r} on the server"
        )


def test_browser_has_wording_for_every_drop_reason():
    """`dropped[]` is shown to the student, so every reason the validator can
    emit needs student-facing wording in the browser. A new reason added here
    without one shows up in the UI as "Could not be verified", which is true but
    useless."""
    app_js = (API_SERVICE.parent.parent / "app.js").read_text()
    block = app_js.split("const DROP_REASONS = {", 1)[1].split("};", 1)[0]
    known = set(re.findall(r"^\s*(\w+):", block, re.MULTILINE))

    from service import resume_evidence

    emitted = {
        value
        for name, value in vars(resume_evidence).items()
        if name.isupper() and isinstance(value, str) and name not in {"SKILL_ID_PREFIX", "CERT_ID_PREFIX", "HONOR_ID_PREFIX"}
    }

    assert emitted <= known, f"no student-facing wording in app.js for: {sorted(emitted - known)}"


def test_both_sources_are_folded_in_academic_then_activities_order():
    """Pins the ordering rule itself. `activities` skills must land *after*
    `academic` skills; swapping them keeps every id resolvable but attaches each
    to the wrong string, which the check above cannot see on its own."""
    profile = AcademicProfile.model_validate(_browser_wire_profile())

    assert profile.skills == ["Python", "SQL", "Git"]
    assert profile.certifications == ["AWS Cloud Practitioner", "CPR"]

    server = collect_evidence_ids(profile)
    assert server["skill_2"].text == "Git"
    assert server["cert_1"].text == "CPR"
