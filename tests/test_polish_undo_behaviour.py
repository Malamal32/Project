"""The browser half of the no-fallback contract.

`tests/test_polish_api.py` proves the service never returns a `description` it
should not. This proves the other half: that the wizard assigns one only inside
the success branch, so a failed polish leaves the student's own words exactly as
typed. Between them there is no path — outage, disabled stage, refused rewrite —
that can blank the textarea.

Runs the real `frontend/js/app.js` under node rather than reimplementing its
state machine, for the same reason `test_evidence_id_parity.py` does: a Python
copy would be a second thing to keep in step.

The component is reached the way Alpine reaches it. `app.js` ends with
`document.addEventListener('alpine:init', ...)` and registers the factory via
`Alpine.data('pathfinder', ...)`, so the harness shims those two and captures
the factory. `fetch` is shimmed too, which means the real `api-service.js` runs
end to end — only the network is fake.

Skipped when node is unavailable (the devcontainer installs Python only).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; frontend behaviour not checked"
)


HARNESS = """
import { pathToFileURL } from 'node:url';

// Alpine's two entry points, shimmed just enough to hand us the factory.
let factory = null;
globalThis.window = globalThis;
globalThis.Alpine = { data: (_name, fn) => { factory = fn; } };
globalThis.document = {
  addEventListener: (_event, cb) => cb(),
  createElement: () => ({ click() {} , appendChild() {} }),
  body: { appendChild() {}, removeChild() {} }
};

const RESPONSE = JSON.parse(process.argv[2]);
const calls = [];
globalThis.fetch = async (url, init) => {
  calls.push({ url, body: JSON.parse(init.body) });
  if (RESPONSE.networkError) throw new Error('connection refused');
  return { ok: RESPONSE.ok !== false, status: RESPONSE.status || 200, json: async () => RESPONSE.body };
};

await import(pathToFileURL(process.argv[3]).href);

const app = factory();
app.addExperience();
const row = app.experience[0];
row.role = 'Help Desk Assistant';
row.employer = 'Riverbend State';
row.start = 'Jun 2024';
row.end = 'Aug 2024';
row.description = 'worked the help desk, wrote a Python script';

await app.polishDescription(row, 'experience');
const afterPolish = { ...row };

app.undoPolish(row);
const afterUndo = { ...row };

console.log(JSON.stringify({ afterPolish, afterUndo, calls }));
"""


def _run(response: dict) -> dict:
    harness = FRONTEND / "js" / "_polish_harness.mjs"
    harness.write_text(HARNESS)
    try:
        proc = subprocess.run(
            ["node", str(harness), json.dumps(response), str(FRONTEND / "js" / "app.js")],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        harness.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


TYPED = "worked the help desk, wrote a Python script"
POLISHED = "Resolved help desk tickets\nWrote a Python script to close stale tickets"


def test_success_replaces_the_description_and_offers_undo():
    result = _run({"body": {"success": True, "description": POLISHED, "warnings": []}})

    assert result["afterPolish"]["description"] == POLISHED
    assert result["afterPolish"]["polishStatus"] == "done"
    assert result["afterPolish"]["descriptionBefore"] == TYPED


def test_undo_restores_the_students_own_words():
    result = _run({"body": {"success": True, "description": POLISHED, "warnings": []}})

    assert result["afterUndo"]["description"] == TYPED
    assert result["afterUndo"]["descriptionBefore"] is None
    assert result["afterUndo"]["polishStatus"] == "idle"


def test_the_card_fields_are_sent_with_the_description():
    """The role, employer and dates go with it — they are what let the rewrite
    say who the work was for, and they widen the source the figure guard checks
    against."""
    result = _run({"body": {"success": True, "description": POLISHED, "warnings": []}})
    sent = result["calls"][0]

    assert sent["url"].endswith("/api/description/polish")
    assert sent["body"]["kind"] == "experience"
    assert sent["body"]["experience"]["title"] == "Help Desk Assistant"
    assert sent["body"]["experience"]["organization"] == "Riverbend State"
    assert sent["body"]["experience"]["dates"] == "Jun 2024 – Aug 2024"


def test_a_failed_polish_leaves_the_text_untouched():
    """The disabled-stage and outage shape: success=False, description=''."""
    result = _run(
        {"body": {"success": False, "description": "", "warnings": ["Polishing is unavailable."]}}
    )

    assert result["afterPolish"]["description"] == TYPED
    assert result["afterPolish"]["polishStatus"] == "failed"
    assert result["afterPolish"]["descriptionBefore"] is None
    assert result["afterPolish"]["polishWarning"] == "Polishing is unavailable."


def test_an_unreachable_service_leaves_the_text_untouched():
    result = _run({"networkError": True})

    assert result["afterPolish"]["description"] == TYPED
    assert result["afterPolish"]["polishStatus"] == "failed"
    assert "unchanged" in result["afterPolish"]["polishWarning"]


def test_an_http_error_leaves_the_text_untouched():
    result = _run({"ok": False, "status": 500, "body": {}})

    assert result["afterPolish"]["description"] == TYPED
    assert result["afterPolish"]["polishStatus"] == "failed"


def test_a_success_with_an_empty_description_cannot_blank_the_field():
    """Belt and braces against the response shape the service promises never to
    send. If it ever did, the student still keeps their words."""
    result = _run({"body": {"success": True, "description": "", "warnings": []}})

    assert result["afterPolish"]["description"] == TYPED
    assert result["afterPolish"]["polishStatus"] == "failed"


def test_a_partial_result_keeps_the_lines_and_shows_the_reason():
    """A guard dropping one line is still a success — the student gets the
    survivors and is told what was removed."""
    warning = "1 drafted line(s) were removed because they stated figures your notes don't mention."
    result = _run({"body": {"success": True, "description": POLISHED, "warnings": [warning]}})

    assert result["afterPolish"]["description"] == POLISHED
    assert result["afterPolish"]["polishStatus"] == "done"
    assert result["afterPolish"]["polishWarning"] == warning
