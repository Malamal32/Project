"""
Market Matching service.

    StudentProfile + MarketProfile  -->  MarketMatch

This layer NEVER touches the database directly. It only accepts a verified
StudentProfile (student-approved data, already reviewed by the student) and
a MarketProfile (already produced by the job-market service, e.g. via
POST /api/market/analyze), and returns a MarketMatch. It has no import of,
or dependency on, any DB/ORM module.

Core rule: market demand (frequency/posting_count) is NEVER itself evidence
that the student has a skill — it only decides which skills are worth
checking. Every "verified"/"coursework"/"transferable" match must carry at
least one evidence ID that points back to a real item in the StudentProfile.
Nothing here writes to the StudentProfile; not_verified skills are reported,
never auto-added.

Two things to keep in mind when editing this file:

- `frontend/js/services/api-service.js` mirrors this module (`matchMarket`,
  `slugify`, `wordMatch`, TOP_N_SKILLS, the same four statuses) so the browser
  can show the student their match without a round trip. That copy is for
  display only — POST /api/resume/generate accepts no MarketMatch and always
  recomputes one here, because the browser assigns its own evidence ids (see
  the note on `GenerateResumeRequest` in service/schemas.py). The two must not
  drift even so: if they disagree, the student is shown one match and the
  resume is built against another. Change both or neither.

- The `StudentProfile` here is NOT `models.student.StudentProfile`. This one is
  a Pydantic request shape carrying experience and projects; that one is the
  SQLAlchemy row, which has neither. Importing both into the same module needs
  an alias.
"""
from __future__ import annotations

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

MatchStatus = Literal["verified", "coursework", "transferable", "not_verified"]


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class StudentSkill(BaseModel):
    id: str
    name: str
    aliases: List[str] = Field(default_factory=list)


class StudentCoursework(BaseModel):
    id: str
    course_code: Optional[str] = None
    course_name: Optional[str] = None
    student_approved: bool = True


class StudentExperience(BaseModel):
    id: str
    description: str = ""


class StudentProject(BaseModel):
    id: str
    technologies: str = ""
    description: str = ""


class StudentProfile(BaseModel):
    """Verified/approved student data only. Everything here is assumed to
    have already been reviewed and approved by the student."""
    skills: List[StudentSkill] = Field(default_factory=list)
    certifications: List[StudentSkill] = Field(default_factory=list)
    coursework: List[StudentCoursework] = Field(default_factory=list)
    experience: List[StudentExperience] = Field(default_factory=list)
    projects: List[StudentProject] = Field(default_factory=list)


class MarketSkill(BaseModel):
    id: Optional[str] = None
    name: str
    posting_count: int = 0
    frequency: float = 0.0


class MarketProfile(BaseModel):
    """Shape returned by the job-market service (POST /api/market/analyze).
    Only the fields this layer needs are modeled here."""
    skills: List[MarketSkill] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class MatchSummary(BaseModel):
    top_market_skills_considered: int
    verified_top_skills: int


class SkillMatch(BaseModel):
    market_skill_id: str
    market_skill: str
    posting_count: int
    frequency: float
    status: MatchStatus
    evidence: List[str] = Field(default_factory=list)


class MarketMatch(BaseModel):
    summary: MatchSummary
    matches: List[SkillMatch]
    gaps: List[str]


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

TOP_N_SKILLS = 10


def _slugify(name: str) -> str:
    return re.sub(r"^_+|_+$", "", re.sub(r"[^a-z0-9]+", "_", name.lower()))


def _word_match(haystack: Optional[str], needle: str) -> bool:
    """Conservative whole-phrase match: needle must appear as a standalone
    word/phrase in haystack, not merely as a substring of a longer word."""
    if not haystack:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(needle.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, haystack.lower()) is not None


def _check_verified(skill_name: str, profile: StudentProfile) -> Optional[List[str]]:
    """Explicit skill or approved alias, exact match only."""
    for item in (*profile.skills, *profile.certifications):
        if item.name.lower() == skill_name.lower():
            return [item.id]
        if any(alias.lower() == skill_name.lower() for alias in item.aliases):
            return [item.id]
    return None


def _check_coursework(skill_name: str, profile: StudentProfile) -> Optional[List[str]]:
    """Coursework must EXPLICITLY name the technology/concept — a vague
    course title (e.g. "Intro to Business") never implies a specific skill
    like "SQL" just because the subject is broadly related."""
    for course in profile.coursework:
        if _word_match(course.course_name, skill_name) or _word_match(course.course_code, skill_name):
            return [course.id]
    return None


def _check_transferable(skill_name: str, profile: StudentProfile) -> Optional[List[str]]:
    """Concrete evidence in experience/project text — still a literal,
    conservative word match, never an inference from a related domain."""
    for exp in profile.experience:
        if _word_match(exp.description, skill_name):
            return [exp.id]
    for proj in profile.projects:
        combined = f"{proj.technologies} {proj.description}"
        if _word_match(combined, skill_name):
            return [proj.id]
    return None


def match_market(student_profile: StudentProfile, market_profile: MarketProfile) -> MarketMatch:
    """Pure function: no DB access, no mutation of either input. Market
    frequency/posting_count are read-only signal for which skills to check —
    they never count as evidence of student ability."""
    top_skills = market_profile.skills[:TOP_N_SKILLS]
    matches: List[SkillMatch] = []

    for skill in top_skills:
        skill_id = skill.id or _slugify(skill.name)

        evidence = _check_verified(skill.name, student_profile)
        status: MatchStatus = "verified"

        if evidence is None:
            evidence = _check_coursework(skill.name, student_profile)
            status = "coursework"

        if evidence is None:
            evidence = _check_transferable(skill.name, student_profile)
            status = "transferable"

        if evidence is None:
            evidence = []
            status = "not_verified"

        matches.append(
            SkillMatch(
                market_skill_id=skill_id,
                market_skill=skill.name,
                posting_count=skill.posting_count,
                frequency=skill.frequency,
                status=status,
                evidence=evidence,
            )
        )

    verified_top_skills = sum(1 for m in matches if m.status != "not_verified")
    gaps = [m.market_skill for m in matches if m.status == "not_verified"]

    return MarketMatch(
        summary=MatchSummary(
            top_market_skills_considered=len(top_skills),
            verified_top_skills=verified_top_skills,
        ),
        matches=matches,
        gaps=gaps,
    )
