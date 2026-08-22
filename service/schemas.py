"""Wire schemas for the transcript service.

Deliberately separate from `models/`, which is the SQLAlchemy ORM layer for the
hiring database. These are the shapes the HTTP API speaks; `service.profile_store`
is the one place that translates between them and the ORM.

Every field is optional because "not present in the document" must be
expressible — the extraction rules forbid inventing a value, so a missing field
comes back null and the student fills it in.
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

# Pure Pydantic, no DB import — safe to pull into the wire layer.
from service.market_matching import MarketProfile

EXTRACTION_METHODS = ("llm", "rules", "manual", "none")


class Coursework(BaseModel):
    id: str
    course_code: Optional[str] = None
    course_name: Optional[str] = None
    credits: Optional[float] = None
    grade: Optional[str] = None
    term: Optional[str] = None
    student_approved: bool = False


class AcademicProfile(BaseModel):
    institution: Optional[str] = None
    degree: Optional[str] = None
    degree_level: Optional[str] = None
    major: Optional[str] = None
    minor: Optional[str] = None
    concentration: Optional[str] = None
    graduation_date: Optional[str] = None
    expected_graduation_date: Optional[str] = None
    gpa: Optional[str] = None
    coursework: List[Coursework] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    honors: List[str] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)


class ParseTextRequest(BaseModel):
    """Body of POST /api/transcript/parse-text.

    `text` is what the browser's pdf.js extractor read out of the document. It
    is treated exactly as hostile as an uploaded file: bounded in size, framed
    as data in the prompt, never logged, never persisted.
    """

    text: str


class ParseResponse(BaseModel):
    success: bool
    academic_profile: AcademicProfile
    warnings: List[str] = Field(default_factory=list)
    review_required: bool = True
    # Which stage produced the profile. Observability only — the frontend
    # behaves identically either way, since every field needs approval.
    # "llm" = Claude API, "rules" = regex fallback, "none" = nothing extracted.
    extraction_method: Literal["llm", "rules", "none"] = "none"


class SaveProfileRequest(BaseModel):
    """Body of POST /api/student/profile.

    Sent *after* the student has reviewed the parsed profile — this is the only
    request in the service that writes to the database. `extraction_method`
    records which stage originally produced the fields; "manual" is legitimate,
    because a student may correct or type in everything themselves.
    """

    academic_profile: AcademicProfile
    extraction_method: Literal["llm", "rules", "manual"] = "manual"


class SaveProfileResponse(BaseModel):
    student_profile_id: str
    courses_saved: int
    attributes_saved: int


# --- LinkedIn data-export import -------------------------------------------
# Request and response shapes for POST /api/linkedin/import. See
# `service/linkedin_import.py` for why the import reads a student's own export
# archive rather than calling the LinkedIn API.
#
# Note what these records do *not* carry: an id. The browser owns the id space
# for experience and projects — `nid()` in `frontend/js/app.js` mints the ids
# the resume request cites — and a server-assigned id here would be a second
# scheme that has to agree with it. There is already one of those in this
# codebase (see `tests/test_evidence_id_parity.py`); a second is not worth an
# import's convenience. Imported entries become wizard rows and get their ids
# the same way typed ones do.


class ImportedExperience(BaseModel):
    """One position from `Positions.csv`, verbatim.

    Dates are separate and unparsed because the wizard's own form has separate
    start/end fields and because "Jun 2024" is what the archive said. See the
    header comment in `models/student.py` on why nothing is normalized on the
    way in.
    """

    title: Optional[str] = None
    organization: Optional[str] = None
    location: Optional[str] = None
    started_on: Optional[str] = None
    finished_on: Optional[str] = None
    description: str = ""


class ImportedProject(BaseModel):
    """One entry from `Projects.csv`. `url` rather than `technologies`: the
    archive has a link column and no technologies column, and inferring a tech
    stack from a description is exactly the kind of guess this codebase drops
    instead of making."""

    name: Optional[str] = None
    url: Optional[str] = None
    started_on: Optional[str] = None
    finished_on: Optional[str] = None
    description: str = ""


class IgnoredFile(BaseModel):
    """A file the import did not read, and why.

    Reported rather than silently dropped, for the same reason `DroppedClaim`
    is: "we skipped this and here is why" is actionable, and an import that
    quietly returns half an archive looks like a student with half a career.
    """

    name: str
    reason: str


class LinkedInImportRequest(BaseModel):
    """Body of POST /api/linkedin/import.

    `files` maps an archive member name to its CSV text. The browser unzips the
    archive and sends only the members this import reads — the same split as
    `/api/transcript/parse-text`, and for a stronger reason: a LinkedIn export
    also contains the student's connections and messages, which are other
    people's personal data and have no business crossing this wire.
    """

    files: Dict[str, str] = Field(default_factory=dict)


class LinkedInImportResponse(BaseModel):
    """Always HTTP 200 for a well-formed request, mirroring ParseResponse.

    `review_required` is not a formality here either: these are records the
    student wrote on another site, at another time, for another audience, and
    every one of them is editable before it reaches a resume.
    """

    success: bool
    experience: List[ImportedExperience] = Field(default_factory=list)
    projects: List[ImportedProject] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)
    honors: List[str] = Field(default_factory=list)
    # Which archive members were actually parsed, in student-facing names, so
    # the review screen can say what it drew from.
    files_read: List[str] = Field(default_factory=list)
    files_ignored: List[IgnoredFile] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    review_required: bool = True


# --- Resume generation -----------------------------------------------------
# Request shapes for POST /api/resume/generate.
#
# Experience, projects, and activities are not part of AcademicProfile and are
# not persisted by `models/student.py` — a transcript does not contain them.
# The student types them into the browser, so they arrive on the request and
# live only for its duration.


class ResumeTargetRole(BaseModel):
    """The role the resume is aimed at. Drives ordering and emphasis only —
    never a source of facts about the student."""

    role: str
    level: Optional[str] = None
    location: Optional[str] = None


class ResumeExperience(BaseModel):
    """`id` is the evidence handle: bullets generated from this item must cite
    it, and the validator rejects a bullet that cites anything else."""

    id: str
    title: Optional[str] = None
    organization: Optional[str] = None
    dates: Optional[str] = None
    description: str = ""


class ResumeProject(BaseModel):
    id: str
    name: Optional[str] = None
    technologies: str = ""
    description: str = ""


class GenerateResumeRequest(BaseModel):
    """Body of POST /api/resume/generate.

    Note what is absent: a MarketMatch. The client mirrors the matcher in JS and
    could send one, but the browser and the server assign different evidence ids
    to the same coursework (positional `course_3` vs. the uuid the extraction
    stage minted), so a client-computed match cites ids the server cannot
    resolve and every claim would be dropped. The match is always recomputed
    here from the profile below, which makes the server's ids the only ids in
    play.
    """

    career: ResumeTargetRole
    academic_profile: AcademicProfile
    experience: List[ResumeExperience] = Field(default_factory=list)
    projects: List[ResumeProject] = Field(default_factory=list)
    market_profile: MarketProfile
    # "Regenerate wording" — same facts, same evidence, different phrasing.
    # Bounded in the endpoint against RESUME_MAX_VARIANT.
    variant: int = 0


# --- Resume generation: response -------------------------------------------
# These models are BOTH the wire response and the structured-output schema the
# model fills in (see service/resume_generation.py). One shape, not two: unlike
# the extraction stage there is no server-assigned field to withhold, so a
# separate model-facing schema would be a mapping layer with nothing to map.


class Claim(BaseModel):
    """One generated statement plus the profile items it rests on.

    `evidence` is the whole guardrail. A claim whose ids do not resolve, or do
    not support the text, is deleted by `service.resume_evidence` before this
    ever reaches the student.
    """

    text: str
    evidence: List[str] = Field(default_factory=list)


class SkillEntry(BaseModel):
    name: str
    evidence: List[str] = Field(default_factory=list)
    # Set when this entry corresponds to a skill the market asked for, so the
    # frontend can show which resume lines are answering real demand.
    market_skill_id: Optional[str] = None


class ExperienceEntry(BaseModel):
    experience_id: str
    bullets: List[Claim] = Field(default_factory=list)


class ProjectEntry(BaseModel):
    project_id: str
    bullets: List[Claim] = Field(default_factory=list)


class EducationBlock(BaseModel):
    """institution/degree_line/graduation_date/gpa are verbatim echoes of the
    profile. The validator compares them character for character and nulls any
    field the model reworded — a "helpfully" reformatted graduation date is
    still a changed fact."""

    institution: Optional[str] = None
    degree_line: Optional[str] = None
    graduation_date: Optional[str] = None
    gpa: Optional[str] = None
    coursework: List[Claim] = Field(default_factory=list)
    honors: List[Claim] = Field(default_factory=list)


class ResumeDocument(BaseModel):
    summary: Claim
    skills: List[SkillEntry] = Field(default_factory=list)
    experience: List[ExperienceEntry] = Field(default_factory=list)
    projects: List[ProjectEntry] = Field(default_factory=list)
    education: EducationBlock = Field(default_factory=EducationBlock)


class DroppedClaim(BaseModel):
    """A claim the validator refused to pass through.

    Returned to the student rather than silently swallowed: "we left this out
    and here is why" is a usable answer, and it makes the guardrail visible
    instead of mysterious.
    """

    section: str
    text: str
    evidence: List[str] = Field(default_factory=list)
    reason: str
    detail: str = ""


class GenerateResumeResponse(BaseModel):
    """Always HTTP 200, mirroring ParseResponse: a refusal or an API outage is
    `success=False` plus a student-facing warning, never a stack trace."""

    success: bool
    # Flat mirror of resume.summary.text. The browser's existing
    # `generateResume()` caller destructures `{ summary }` and nothing else, so
    # this keeps the mock swap to a one-line fetch().
    summary: str = ""
    resume: Optional[ResumeDocument] = None
    dropped: List[DroppedClaim] = Field(default_factory=list)
    # Market skills with no support anywhere in the profile. Shown as gaps —
    # never folded into the resume.
    gaps: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    model_version: Optional[str] = None
    variant: int = 0


# --- Description polish ----------------------------------------------------
# One experience or project description, rewritten into resume lines, from a
# button on the wizard step where the student typed it. See
# `service/description_polish.py` for why this stage has two guards rather than
# leaning on the evidence contract above.


class PolishDescriptionRequest(BaseModel):
    """Body of POST /api/description/polish. One item per request.

    The item is carried as a whole `ResumeExperience` / `ResumeProject` rather
    than a bare description string, and that is deliberate: the title,
    organization and dates are what let the rewrite say "for whom" instead of
    only "what", and — less obviously — they widen the source text the guards
    check against, so a line that legitimately echoes a year from the dates is
    not read as an invented figure.

    Reusing the resume stage's own models means what the polish stage sees for a
    row is byte-identical to what the resume stage will see for that same row
    later. One adapter serves both in `frontend/js/services/api-service.js`, so
    the two calls cannot drift on how a start/end pair becomes `dates`.

    `id` is unused here — this stage mints no evidence and is never logged with
    it — and is carried only because it is part of that one shape.
    """

    kind: Literal["experience", "project"]
    experience: Optional[ResumeExperience] = None
    project: Optional[ResumeProject] = None

    @model_validator(mode="after")
    def _item_matches_kind(self) -> "PolishDescriptionRequest":
        item = self.experience if self.kind == "experience" else self.project
        if item is None:
            raise ValueError(f"kind is {self.kind!r} but no {self.kind} was provided")
        return self


class PolishedDescription(BaseModel):
    """What the model fills in. A list, not a paragraph: the field it is
    rewriting is captioned "One line per accomplishment", and asking for lines
    is what stops the model returning one dense sentence with three clauses.

    The service joins these with newlines before responding. The browser never
    joins, because the joined string *is* the evidence text every later resume
    bullet for this item is checked against — and a second place that decides
    how it is assembled is a second place for it to be assembled differently.
    """

    lines: List[str] = Field(default_factory=list)


class PolishDescriptionResponse(BaseModel):
    """Always HTTP 200, like the other two model-backed endpoints.

    `description` is `""` on every failure path, and the frontend depends on
    that: it assigns only when `success` and `description` are both truthy, so
    there is no response this service can send that blanks what the student
    typed.
    """

    success: bool
    description: str = ""
    warnings: List[str] = Field(default_factory=list)
    model_version: Optional[str] = None
