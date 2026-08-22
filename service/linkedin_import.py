"""LinkedIn data export -> the work history the wizard could otherwise only be typed.

Work experience is the one part of a resume this product had no source for. A
transcript does not contain it, so `models/student.py` does not store it and
`service/schemas.py` carries it only for the lifetime of a resume request. This
module gives that field a source, and keeps it request-scoped for exactly the
same reason: nothing here is persisted, and only what the student keeps on the
review screen reaches `POST /api/resume/generate`.

**Why the export archive and not the LinkedIn API.** "Sign In with LinkedIn"
(OpenID Connect) returns a name, an email, and a picture — no positions. Profile
positions live behind the LinkedIn Partner Programs, gated on an approved
business relationship this product does not have, and scraping a profile page
violates the user agreement and is precisely the behaviour `pipeline/allowlist.py`
exists to forbid on the supply side. The student's own "Get a copy of your data"
archive is consented, complete and already structured: no key, no scraping, no
partner review, and the student can see exactly what they handed over.

**Why no model call.** These are CSV columns, not prose to be read. Putting an
extractor in front of them would add a way to be wrong and nothing else. That
also means this stage has no degraded mode and needs none — where the transcript
path falls back to regex because a worse reading of a document still beats no
reading, an unreadable CSV here has no worse reading to offer. It says which
file and why, and the student types the entry in as they always could.

**What is read.** `EXPORT_FILES` below, and nothing else. The archive also
contains `Connections.csv`, `messages.csv`, ad-targeting segments and inferred
attributes — other people's personal data alongside the student's, none of it
resume material. `frontend/js/services/linkedin-import.js` drops those members
in the browser so they never cross the wire, and this module refuses them again
on arrival. Two independent checks, because a single filename list standing
between that data and an HTTP request is a single point of failure.

Stdlib `csv` only, no native dependencies: this endpoint runs on the Worker,
unlike the transcript upload path.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import structlog

from service.config import (
    MAX_LINKEDIN_FIELD_CHARS,
    MAX_LINKEDIN_FILE_CHARS,
    MAX_LINKEDIN_ROWS,
)
from service.schemas import (
    IgnoredFile,
    ImportedExperience,
    ImportedProject,
    LinkedInImportResponse,
)

log = structlog.get_logger()

# How far into a file to look for the header row. Some archive vintages put a
# notes preamble above it, so the header is not reliably line 1 — but it is
# always near the top, and scanning further would start matching data rows.
_HEADER_SEARCH_ROWS = 10


@dataclass(frozen=True)
class _FileSpec:
    """One whitelisted export file: what it becomes, and which columns we read.

    `columns` maps our field name to the header spellings LinkedIn has used for
    it. A list rather than one pinned string because the column names have
    changed between archive vintages, and a student's archive is whatever
    LinkedIn generated for them at the time, not whatever is current. A header
    we do not recognise is reported to the student, never guessed at.

    `identifies` are the fields that make a row worth keeping. They double as
    the header signature: the header row is the first one that names any of
    them, and a row that fills none of them is an empty record with a stray
    timestamp on it.
    """

    kind: str
    label: str
    columns: Mapping[str, Tuple[str, ...]]
    identifies: Tuple[str, ...]


_POSITIONS = _FileSpec(
    kind="positions",
    label="Positions.csv",
    columns={
        "organization": ("company name", "company"),
        "title": ("title", "position"),
        "description": ("description",),
        "location": ("location",),
        "started_on": ("started on", "start date", "started"),
        "finished_on": ("finished on", "end date", "finished"),
    },
    identifies=("title", "organization"),
)

_PROJECTS = _FileSpec(
    kind="projects",
    label="Projects.csv",
    columns={
        "name": ("title", "name", "project name"),
        "description": ("description",),
        "url": ("url", "link"),
        "started_on": ("started on", "start date", "started"),
        "finished_on": ("finished on", "end date", "finished"),
    },
    identifies=("name",),
)

# The three list files share a shape: one column of names, one row each. They
# are kept as separate specs rather than one parameterised spec so the header
# aliases stay per-file — "Title" means an honor here and a job title in
# Positions.csv, and collapsing them would make that collision invisible.
_SKILLS = _FileSpec(
    kind="skills",
    label="Skills.csv",
    columns={"name": ("name", "skill")},
    identifies=("name",),
)

_CERTIFICATIONS = _FileSpec(
    kind="certifications",
    # Only the name. A resume lists "AWS Certified Cloud Practitioner", not the
    # authority and licence number beside it in the archive, and importing
    # fields the student will only delete is not a favour.
    label="Certifications.csv",
    columns={"name": ("name", "title")},
    identifies=("name",),
)

_HONORS = _FileSpec(
    kind="honors",
    label="Honors.csv",
    columns={"name": ("title", "name")},
    identifies=("name",),
)

# Keyed by lowercased basename. Both spellings of Honors map to one spec: the
# archive uses the US spelling, but the file has shipped as Honours.csv in some
# locales and an import that silently skipped it would look like a student with
# no honors.
EXPORT_FILES: Dict[str, _FileSpec] = {
    "positions.csv": _POSITIONS,
    "projects.csv": _PROJECTS,
    "skills.csv": _SKILLS,
    "certifications.csv": _CERTIFICATIONS,
    "honors.csv": _HONORS,
    "honours.csv": _HONORS,
}

# Processing order, so the response is identical whatever order the client
# happened to send the files in. Two different orderings of the same archive
# producing two different resumes would be an unpleasant thing to debug.
_SPEC_ORDER: Tuple[_FileSpec, ...] = (_POSITIONS, _PROJECTS, _SKILLS, _CERTIFICATIONS, _HONORS)


def member_basename(name: str) -> str:
    """The lookup key for an archive member path.

    Members arrive as full paths in some archives and bare names in others, and
    Windows-built zips use backslashes. Only the basename is ever matched, and
    it is matched against `EXPORT_FILES` — a path is never used to open
    anything, so there is no traversal to defend against here.
    """
    return name.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


def is_wanted(name: str) -> bool:
    """Whether this archive member is one of the files the import reads."""
    return member_basename(name) in EXPORT_FILES


def _normalize_header(cell: str) -> str:
    return cell.strip().lstrip("﻿").lower()


def _find_header(rows: Sequence[Sequence[str]], spec: _FileSpec) -> Optional[int]:
    """Index of the header row, or None if no row names an identifying column."""
    wanted = {alias for field in spec.identifies for alias in spec.columns[field]}
    for index, row in enumerate(rows[:_HEADER_SEARCH_ROWS]):
        if any(_normalize_header(cell) in wanted for cell in row):
            return index
    return None


def _read_rows(text: str, spec: _FileSpec) -> Tuple[List[Dict[str, str]], List[str], Optional[str]]:
    """CSV text -> rows keyed by *our* field names, plus warnings, plus a reason
    the file was unusable if it was.

    Columns we do not know about are dropped here rather than carried along:
    the archive's list files in particular sit next to columns we have no use
    for, and the only thing forwarding them would achieve is putting more of the
    student's data into the next request.
    """
    try:
        raw_rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error:
        return [], [], "could not be read as CSV"

    header_index = _find_header(raw_rows, spec)
    if header_index is None:
        return [], [], "does not have the columns this import expects"

    header = [_normalize_header(cell) for cell in raw_rows[header_index]]

    # our field -> column index. First match wins, so a file carrying both an
    # old and a new spelling of the same column reads the leftmost one.
    positions: Dict[str, int] = {}
    for field, aliases in spec.columns.items():
        for index, name in enumerate(header):
            if name in aliases:
                positions[field] = index
                break

    warnings: List[str] = []
    body = raw_rows[header_index + 1 :]
    if len(body) > MAX_LINKEDIN_ROWS:
        warnings.append(
            f"{spec.label} has more than {MAX_LINKEDIN_ROWS} entries; "
            f"the first {MAX_LINKEDIN_ROWS} were imported."
        )
        body = body[:MAX_LINKEDIN_ROWS]

    rows: List[Dict[str, str]] = []
    for row in body:
        record = {
            field: row[index].strip()
            for field, index in positions.items()
            if index < len(row) and row[index].strip()
        }
        # A row that fills none of the identifying columns is not a partial
        # record to salvage — it is a blank line or a trailing artefact.
        if any(record.get(field) for field in spec.identifies):
            rows.append(record)

    return rows, warnings, None


def _truncate(value: str, label: str, warnings: List[str]) -> str:
    """Bound one free-text field, and say so.

    A LinkedIn description is whatever the student pasted into it, and it ends
    up in the resume prompt. Truncation is always announced, for the same reason
    MAX_LLM_INPUT_CHARS is: a silently shortened description reads as a model
    that ignored half the job.
    """
    if len(value) <= MAX_LINKEDIN_FIELD_CHARS:
        return value
    warnings.append(f"The description for “{label}” was long and has been shortened.")
    return value[:MAX_LINKEDIN_FIELD_CHARS]


def _dedupe(values: Sequence[str]) -> List[str]:
    """Case-insensitive, order-preserving. The archive can list the same skill
    twice when it was endorsed under two spellings; the student should not have
    to delete the duplicate we handed them."""
    seen = set()
    out: List[str] = []
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _experience(rows: Sequence[Mapping[str, str]], warnings: List[str]) -> List[ImportedExperience]:
    """Dates stay exactly as the archive wrote them — "Jun 2024", not a parsed
    date. Same rule as `models/student.py`: normalising on the way in discards
    what the source actually said, and the student is about to review it anyway.
    """
    out: List[ImportedExperience] = []
    for row in rows:
        label = row.get("title") or row.get("organization") or "this role"
        out.append(
            ImportedExperience(
                title=row.get("title"),
                organization=row.get("organization"),
                location=row.get("location"),
                started_on=row.get("started_on"),
                finished_on=row.get("finished_on"),
                description=_truncate(row.get("description", ""), label, warnings),
            )
        )
    return out


def _projects(rows: Sequence[Mapping[str, str]], warnings: List[str]) -> List[ImportedProject]:
    out: List[ImportedProject] = []
    for row in rows:
        label = row.get("name") or "this project"
        out.append(
            ImportedProject(
                name=row.get("name"),
                url=row.get("url"),
                started_on=row.get("started_on"),
                finished_on=row.get("finished_on"),
                description=_truncate(row.get("description", ""), label, warnings),
            )
        )
    return out


def _nothing_found_warnings(response: LinkedInImportResponse) -> List[str]:
    """Student-facing notes about what the archive did not yield.

    An import that reads the files and finds nothing looks identical to one that
    silently failed, so it says which it was.
    """
    warnings: List[str] = []
    if not response.files_read:
        warnings.append(
            "We didn't find any of the files we can read in that archive. Make sure "
            "you picked the .zip LinkedIn emailed you, and not a folder or a "
            "screenshot of your profile."
        )
        return warnings
    if not response.experience:
        warnings.append(
            "No work experience was found in that export. You can add entries below."
        )
    return warnings


def parse_export(files: Mapping[str, str]) -> LinkedInImportResponse:
    """Pure function: whitelisted CSV texts in, reviewable records out.

    Persists nothing, sends nothing anywhere, and never raises for bad input —
    an unreadable file becomes an entry in `files_ignored` with the reason, and
    the rest of the archive still imports. A student whose Projects.csv is
    malformed should still get their positions.
    """
    provided: Dict[str, Tuple[str, str]] = {}
    ignored: List[IgnoredFile] = []

    for raw_name, text in files.items():
        key = member_basename(raw_name)
        spec = EXPORT_FILES.get(key)
        if spec is None:
            ignored.append(
                IgnoredFile(name=raw_name, reason="not one of the files this import reads")
            )
            continue
        if len(text) > MAX_LINKEDIN_FILE_CHARS:
            ignored.append(
                IgnoredFile(name=raw_name, reason="larger than this import accepts")
            )
            continue
        # Both Honors spellings resolve to one spec; if an archive somehow had
        # both, the first one wins rather than the pair being concatenated.
        provided.setdefault(spec.kind, (raw_name, text))

    experience: List[ImportedExperience] = []
    projects: List[ImportedProject] = []
    skills: List[str] = []
    certifications: List[str] = []
    honors: List[str] = []
    files_read: List[str] = []
    warnings: List[str] = []

    for spec in _SPEC_ORDER:
        entry = provided.get(spec.kind)
        if entry is None:
            continue
        raw_name, text = entry

        rows, row_warnings, error = _read_rows(text, spec)
        if error is not None:
            ignored.append(IgnoredFile(name=raw_name, reason=error))
            continue

        files_read.append(spec.label)
        warnings.extend(row_warnings)

        if spec.kind == "positions":
            experience = _experience(rows, warnings)
        elif spec.kind == "projects":
            projects = _projects(rows, warnings)
        else:
            names = _dedupe([row["name"] for row in rows if row.get("name")])
            if spec.kind == "skills":
                skills = names
            elif spec.kind == "certifications":
                certifications = names
            else:
                honors = names

    response = LinkedInImportResponse(
        success=True,
        experience=experience,
        projects=projects,
        skills=skills,
        certifications=certifications,
        honors=honors,
        files_read=files_read,
        files_ignored=ignored,
        warnings=warnings,
    )
    response.warnings.extend(_nothing_found_warnings(response))

    # Counts only. The contents of the archive are never logged, on the same
    # terms as a transcript's text.
    log.info(
        "linkedin.imported",
        files_read=len(files_read),
        files_ignored=len(ignored),
        experience=len(experience),
        projects=len(projects),
    )
    return response
