"""PII redaction for posting description text (hard constraint: never store
recruiter names, emails, or phone numbers from postings). Applied at ingestion,
before text is landed as the canonical `posting_description` raw_documents row
that description_raw_ref points to.

Best-effort by nature: email/phone patterns are reliable; recruiter-name
detection only catches names that follow a labeled context (e.g. "Recruiter:
Jane Doe") since free-text name detection without a labeled cue is unreliable
without an NER model. `validate_redacted` is the "tested regex + validation
pass" the constraint calls for — it re-scans the output and fails loudly if an
email or phone slipped through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)")
RECRUITER_LABEL_RE = re.compile(
    r"\b(?:Recruiter|Contact|HR Contact|Talent Partner|Talent Acquisition|"
    r"Hiring Manager|Point of Contact)\s*:\s*"
    r"(?P<name>[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,3})"
)

REDACTION_TOKENS = {
    "email": "[REDACTED_EMAIL]",
    "phone": "[REDACTED_PHONE]",
    "recruiter_name": "[REDACTED_NAME]",
}


@dataclass(frozen=True)
class RedactionMatch:
    kind: str
    start: int
    end: int
    original: str


def _find_matches(text: str) -> list[RedactionMatch]:
    matches: list[RedactionMatch] = []
    for m in RECRUITER_LABEL_RE.finditer(text):
        matches.append(RedactionMatch("recruiter_name", m.start("name"), m.end("name"), m.group("name")))
    for m in EMAIL_RE.finditer(text):
        matches.append(RedactionMatch("email", m.start(), m.end(), m.group()))
    for m in PHONE_RE.finditer(text):
        matches.append(RedactionMatch("phone", m.start(), m.end(), m.group()))

    matches.sort(key=lambda m: m.start)
    filtered: list[RedactionMatch] = []
    last_end = -1
    for m in matches:
        if m.start >= last_end:
            filtered.append(m)
            last_end = m.end
    return filtered


def redact_pii(text: str) -> tuple[str, list[RedactionMatch]]:
    """Returns (redacted_text, matches). matches reference offsets in the *original* text."""
    matches = _find_matches(text)
    if not matches:
        return text, []

    parts: list[str] = []
    cursor = 0
    for m in matches:
        parts.append(text[cursor : m.start])
        parts.append(REDACTION_TOKENS[m.kind])
        cursor = m.end
    parts.append(text[cursor:])
    return "".join(parts), matches


def validate_redacted(text: str) -> None:
    """Validation pass: fails loudly if an email or phone number survived redaction."""
    if EMAIL_RE.search(text):
        raise ValueError("redact_pii output still contains an email address match")
    if PHONE_RE.search(text):
        raise ValueError("redact_pii output still contains a phone number match")
