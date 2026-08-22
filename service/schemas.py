"""Wire schemas for the transcript service.

Deliberately separate from `models/`, which is the SQLAlchemy ORM layer for the
hiring database. These are the shapes the HTTP API speaks; `service.profile_store`
is the one place that translates between them and the ORM.

Every field is optional because "not present in the document" must be
expressible — the extraction rules forbid inventing a value, so a missing field
comes back null and the student fills it in.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

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
