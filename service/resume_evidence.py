"""The evidence contract for generated resumes.

Every claim the model produces carries a list of evidence ids. This module is
what makes that list mean something: it resolves each id against the student's
actual profile, checks that the claim is supported by what those items say, and
deletes anything that fails. A claim that survives can be pointed at a real line
of the student's record.

This is the resume-side counterpart of the guardrail PROMPT.md specifies for
Phase 5 extraction ("post-validate that description_raw[start:end] ==
evidence_excerpt exactly ... an extraction without verifiable evidence is a
hallucination"). Same principle, different substrate: there the anchor is a
character offset into the posting, here it is an id into the profile.

Nothing here raises and nothing here calls the model. Failures drop the
offending claim, record why, and let the rest of the resume through — a student
with a slightly shorter resume is better served than one staring at an error,
and far better served than one handed a fabricated bullet.

What this module does NOT do: judge whether a surviving claim is a *good* line.
It checks that the claim is traceable, not that it is well written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import structlog

from service.market_matching import (
    MarketMatch,
    MarketProfile,
    StudentCoursework,
    StudentExperience,
    StudentProject,
    StudentSkill,
    _word_match,
    match_market,
)

# Aliased: `models.student.StudentProfile` is the SQLAlchemy row and this is the
# matcher's Pydantic request shape. Same name, unrelated types.
from service.market_matching import StudentProfile as MatchStudentProfile
from service.schemas import (
    AcademicProfile,
    Claim,
    DroppedClaim,
    EducationBlock,
    ExperienceEntry,
    ProjectEntry,
    ResumeDocument,
    ResumeExperience,
    ResumeProject,
    SkillEntry,
)
from service.text_guards import unsupported_numbers

log = structlog.get_logger()


# Drop reasons. Strings rather than an enum because they cross the wire to the
# frontend and end up in log lines; the set is closed here for grep-ability.
NO_EVIDENCE = "no_evidence"
UNKNOWN_EVIDENCE_ID = "unknown_evidence_id"
EVIDENCE_OUT_OF_SCOPE = "evidence_out_of_scope"
NOT_VERIFIED_SKILL = "not_verified_skill"
GAP_SKILL_MENTIONED = "gap_skill_mentioned"
UNSUPPORTED_NUMBER = "unsupported_number"
VERBATIM_MISMATCH = "verbatim_mismatch"
DUPLICATE = "duplicate"

# AcademicProfile stores skills, certifications, and honors as bare strings, so
# their evidence ids are positional and minted here. Both `collect_evidence_ids`
# and `to_match_profile` build ids from these prefixes and must keep agreeing:
# the matcher's evidence ids are handed to the model, and the validator resolves
# what the model cites. If the two ever disagree, every skill claim is dropped.
SKILL_ID_PREFIX = "skill"
CERT_ID_PREFIX = "cert"
HONOR_ID_PREFIX = "honor"


@dataclass(frozen=True)
class EvidenceItem:
    """One citable item from the student's profile.

    Two fields because the two checks want different strings:

    `text` is everything the item says, concatenated. It is the haystack the
    number check searches, so it must include every field a claim could
    legitimately draw a figure from — a course's credits and grade included.

    `display_forms` are the renderings that count as reproducing this item
    verbatim. A course legitimately appears as "Database Systems" or as
    "CS 340 Database Systems", and both are faithful; "Advanced Database
    Systems & Design" is not. Comparing against `text` instead would reject
    every honest rendering, because `text` has the grade glued on the end.
    """

    id: str
    kind: str
    text: str
    display_forms: Tuple[str, ...] = ()


def _norm(value: Optional[str]) -> str:
    """Compare-normalization: case and surrounding whitespace only.

    Deliberately does not strip punctuation or collapse inner whitespace. This
    feeds the skills admissibility check, where "C++" and "C" must stay
    different answers.
    """
    return (value or "").strip().lower()


def collect_evidence_ids(
    profile: AcademicProfile,
    experience: Sequence[ResumeExperience] = (),
    projects: Sequence[ResumeProject] = (),
) -> Dict[str, EvidenceItem]:
    """Build the set of ids a claim is allowed to cite.

    Coursework keeps whatever id the extraction stage minted for it. Skills,
    certifications, and honors are bare strings in AcademicProfile, so they get
    positional ids here — which makes this function the single definition of
    what `skill_0` means. Experience and project ids come from the request,
    because the student typed those items in the browser.
    """
    items: Dict[str, EvidenceItem] = {}

    for course in profile.coursework:
        # Skip a course with no id rather than invent one: an id collision would
        # let a bullet cite the wrong course and still validate.
        if not course.id:
            continue
        parts = [course.course_code, course.course_name, course.grade, course.term]
        if course.credits is not None:
            parts.append(str(course.credits))
        code, name = course.course_code, course.course_name
        forms = [f for f in (name, code, f"{code} {name}" if code and name else None) if f]
        items[course.id] = EvidenceItem(
            id=course.id,
            kind="coursework",
            text=" ".join(p for p in parts if p),
            display_forms=tuple(forms),
        )

    for prefix, kind, values in (
        (SKILL_ID_PREFIX, "skill", profile.skills),
        (CERT_ID_PREFIX, "certification", profile.certifications),
        (HONOR_ID_PREFIX, "honor", profile.honors),
    ):
        for index, value in enumerate(values):
            item_id = f"{prefix}_{index}"
            items[item_id] = EvidenceItem(
                id=item_id, kind=kind, text=value or "", display_forms=((value,) if value else ())
            )

    for item in experience:
        if not item.id:
            continue
        parts = [item.title, item.organization, item.dates, item.description]
        items[item.id] = EvidenceItem(
            id=item.id, kind="experience", text=" ".join(p for p in parts if p)
        )

    for item in projects:
        if not item.id:
            continue
        parts = [item.name, item.technologies, item.description]
        items[item.id] = EvidenceItem(
            id=item.id, kind="project", text=" ".join(p for p in parts if p)
        )

    # The education block's verbatim fields are citable too, so a summary
    # sentence naming the institution or degree has something legitimate to
    # point at.
    for field, value in (
        ("institution", profile.institution),
        ("degree", profile.degree),
        ("degree_level", profile.degree_level),
        ("major", profile.major),
        ("minor", profile.minor),
        ("concentration", profile.concentration),
        ("graduation_date", profile.graduation_date),
        ("expected_graduation_date", profile.expected_graduation_date),
        ("gpa", profile.gpa),
    ):
        if value:
            items[f"edu_{field}"] = EvidenceItem(
                id=f"edu_{field}", kind="education", text=value, display_forms=(value,)
            )

    return items


def run_match(
    profile: AcademicProfile,
    market_profile: MarketProfile,
    experience: Sequence[ResumeExperience] = (),
    projects: Sequence[ResumeProject] = (),
) -> MarketMatch:
    """Run the market matcher over a wire-shaped profile.

    Lives here, beside `collect_evidence_ids`, because both mint the same
    positional ids and the whole scheme has to be defined in one place. The
    matcher's `evidence` ids are shown to the model as the ids it should cite,
    and the validator resolves those citations against `collect_evidence_ids` —
    so if these two functions ever disagree about what `skill_0` means, every
    skill claim gets dropped as an unknown id.

    The matcher's `StudentProfile` is a different type from the ORM row of the
    same name in `models/student.py`; it is aliased on import for that reason.
    """
    return match_market(
        MatchStudentProfile(
            skills=[
                StudentSkill(id=f"{SKILL_ID_PREFIX}_{i}", name=v)
                for i, v in enumerate(profile.skills)
                if v
            ],
            certifications=[
                StudentSkill(id=f"{CERT_ID_PREFIX}_{i}", name=v)
                for i, v in enumerate(profile.certifications)
                if v
            ],
            coursework=[
                StudentCoursework(
                    id=c.id, course_code=c.course_code, course_name=c.course_name
                )
                for c in profile.coursework
                if c.id
            ],
            experience=[
                StudentExperience(id=e.id, description=e.description)
                for e in experience
                if e.id
            ],
            projects=[
                StudentProject(
                    id=p.id, technologies=p.technologies, description=p.description
                )
                for p in projects
                if p.id
            ],
        ),
        market_profile,
    )


class _Validator:
    """Holds the per-request lookups so each check reads as one condition.

    Accumulates drops rather than raising, so one bad claim never costs the
    student the rest of the resume.
    """

    def __init__(
        self,
        evidence: Dict[str, EvidenceItem],
        market_match: MarketMatch,
        profile: AcademicProfile,
    ) -> None:
        self.evidence = evidence
        self.profile = profile
        self.dropped: List[DroppedClaim] = []

        # Skills the market wants and the student has no support for. These may
        # not appear anywhere in the resume, in any wording.
        self.gap_terms = [
            m.market_skill for m in market_match.matches if m.status == "not_verified"
        ]
        # Skills the market wants and the student *does* have support for,
        # indexed for the admissibility check.
        self.supported_market = {
            _norm(m.market_skill): m
            for m in market_match.matches
            if m.status != "not_verified"
        }
        # Everything the student explicitly listed as a skill or certification.
        self.claimed_skills = {
            _norm(v) for v in (*profile.skills, *profile.certifications) if v
        }

    # --- recording ---------------------------------------------------------

    def _drop(
        self, section: str, text: str, evidence: List[str], reason: str, detail: str = ""
    ) -> None:
        self.dropped.append(
            DroppedClaim(
                section=section,
                text=text,
                evidence=list(evidence),
                reason=reason,
                detail=detail,
            )
        )
        # Reason and section only. The claim text is derived from the student's
        # own record, and this service does not log student content — the full
        # text goes back in the response, which is their own session.
        log.warning(
            "resume.claim_dropped", section=section, reason=reason, evidence=evidence
        )

    # --- checks ------------------------------------------------------------

    def _evidence_text(self, ids: Sequence[str]) -> str:
        return " ".join(self.evidence[i].text for i in ids if i in self.evidence)

    def _check_gap_leak(self, section: str, text: str, evidence: List[str]) -> bool:
        """A `not_verified` skill must not appear in any generated prose.

        Uses the matcher's own whole-phrase matcher, so this agrees with how the
        skill was judged missing in the first place: if `_word_match` did not
        find "SQL" in the profile, "SQL" may not appear in the resume.

        The escape hatch matters. The matcher is deliberately literal and does no
        stemming, so it scores "REST APIs" a gap for a student whose project says
        "Built a REST API". Blocking on the term alone would then delete a true
        sentence — the guard would be censoring the student's own record. So a
        claim survives if the term does appear in the evidence it cites: that is
        not a model inventing a skill, it is the model quoting the profile. The
        guard still catches the case it exists for, a gap term in a claim whose
        own evidence never mentions it.
        """
        for term in self.gap_terms:
            if not _word_match(text, term):
                continue
            if _word_match(self._evidence_text(evidence), term):
                continue
            self._drop(
                section,
                text,
                evidence,
                GAP_SKILL_MENTIONED,
                f"mentions {term!r}, which the cited evidence does not support",
            )
            return False
        return True

    def _check_claim(
        self,
        section: str,
        claim: Claim,
        *,
        required_id: Optional[str] = None,
    ) -> bool:
        """The checks every generated statement passes, in cheapest-first order."""
        text = claim.text or ""
        evidence = list(claim.evidence or [])

        if not text.strip():
            return False

        if not evidence:
            self._drop(section, text, evidence, NO_EVIDENCE, "no evidence cited")
            return False

        unknown = [i for i in evidence if i not in self.evidence]
        if unknown:
            self._drop(
                section,
                text,
                evidence,
                UNKNOWN_EVIDENCE_ID,
                f"cites unknown id(s): {', '.join(sorted(unknown))}",
            )
            return False

        # A bullet under an experience must cite that experience. Without this a
        # bullet could describe job A while citing job B and still resolve.
        if required_id is not None and required_id not in evidence:
            self._drop(
                section,
                text,
                evidence,
                EVIDENCE_OUT_OF_SCOPE,
                f"does not cite its own source {required_id!r}",
            )
            return False

        if not self._check_gap_leak(section, text, evidence):
            return False

        unsupported = unsupported_numbers(text, self._evidence_text(evidence))
        if unsupported:
            self._drop(
                section,
                text,
                evidence,
                UNSUPPORTED_NUMBER,
                f"figure(s) not in the cited evidence: {', '.join(unsupported)}",
            )
            return False

        return True

    def _check_verbatim(self, section: str, claim: Claim) -> bool:
        """For fields the model may select but not reword.

        Coursework and honours are chosen for relevance, but the text has to be
        the profile's own string. A reworded course title is a small lie that
        looks like a formatting choice.
        """
        if not self._check_claim(section, claim):
            return False
        forms = [
            form
            for i in claim.evidence
            if i in self.evidence
            for form in self.evidence[i].display_forms
        ]
        if not any(_norm(claim.text) == _norm(f) for f in forms):
            self._drop(
                section,
                claim.text,
                list(claim.evidence),
                VERBATIM_MISMATCH,
                "text does not match the cited profile entry exactly",
            )
            return False
        return True

    # --- sections ----------------------------------------------------------

    def _skills(self, entries: List[SkillEntry]) -> List[SkillEntry]:
        kept: List[SkillEntry] = []
        seen = set()
        for entry in entries:
            name = entry.name or ""
            as_claim = Claim(text=name, evidence=entry.evidence)

            if not self._check_claim("skills", as_claim):
                continue

            key = _norm(name)
            if key in seen:
                self._drop("skills", name, list(entry.evidence), DUPLICATE, "repeated skill")
                continue

            # Admissible only if the student listed it, or the matcher found
            # support for it as a market skill. Anything else is the model
            # deciding the student has a skill.
            match = self.supported_market.get(key)
            if key not in self.claimed_skills and match is None:
                self._drop(
                    "skills",
                    name,
                    list(entry.evidence),
                    NOT_VERIFIED_SKILL,
                    "neither listed by the student nor a market skill with support",
                )
                continue

            # When it is a market skill, the evidence must be the evidence the
            # matcher actually found — not some other profile item.
            if match is not None and key not in self.claimed_skills:
                allowed = set(match.evidence)
                if not set(entry.evidence).issubset(allowed):
                    self._drop(
                        "skills",
                        name,
                        list(entry.evidence),
                        NOT_VERIFIED_SKILL,
                        "cites evidence the matcher did not accept for this skill",
                    )
                    continue

            seen.add(key)
            kept.append(entry)
        return kept

    def _bullets(self, section: str, bullets: List[Claim], owner_id: str) -> List[Claim]:
        kept: List[Claim] = []
        seen = set()
        for bullet in bullets:
            if not self._check_claim(section, bullet, required_id=owner_id):
                continue
            key = _norm(bullet.text)
            if key in seen:
                self._drop(section, bullet.text, list(bullet.evidence), DUPLICATE, "repeated bullet")
                continue
            seen.add(key)
            kept.append(bullet)
        return kept

    def _education(self, block: EducationBlock) -> EducationBlock:
        """Verbatim fields are compared against the profile and nulled on any
        difference. Selection is the model's job here; wording is not."""
        checks = (
            ("institution", block.institution, self.profile.institution),
            ("degree_line", block.degree_line, self.profile.degree),
            (
                "graduation_date",
                block.graduation_date,
                self.profile.graduation_date or self.profile.expected_graduation_date,
            ),
            ("gpa", block.gpa, self.profile.gpa),
        )
        resolved = {}
        for field, produced, expected in checks:
            if produced is None:
                resolved[field] = None
                continue
            if expected is not None and _norm(produced) == _norm(expected):
                # Store the profile's string, not the model's — identical after
                # normalization still allows a case or spacing change through.
                resolved[field] = expected
            else:
                resolved[field] = None
                self._drop(
                    "education",
                    produced,
                    [],
                    VERBATIM_MISMATCH,
                    f"{field} does not match the profile",
                )

        return EducationBlock(
            **resolved,
            coursework=[c for c in block.coursework if self._check_verbatim("education", c)],
            honors=[h for h in block.honors if self._check_verbatim("education", h)],
        )


def validate_document(
    document: ResumeDocument,
    profile: AcademicProfile,
    market_match: MarketMatch,
    experience: Sequence[ResumeExperience] = (),
    projects: Sequence[ResumeProject] = (),
) -> Tuple[ResumeDocument, List[DroppedClaim]]:
    """Check every claim and return a cleaned copy plus what was removed.

    The input document is never mutated — the caller keeps whatever the model
    returned, which matters when reading logs after the fact.
    """
    evidence = collect_evidence_ids(profile, experience, projects)
    validator = _Validator(evidence, market_match, profile)

    known_experience = {e.id for e in experience}
    known_projects = {p.id for p in projects}

    summary = document.summary
    kept_summary = summary if validator._check_claim("summary", summary) else Claim(text="")

    kept_experience: List[ExperienceEntry] = []
    for entry in document.experience:
        # An entry for an experience the student never submitted is itself a
        # fabrication, regardless of what its bullets cite.
        if entry.experience_id not in known_experience:
            validator._drop(
                "experience",
                entry.experience_id,
                [entry.experience_id],
                UNKNOWN_EVIDENCE_ID,
                "no such experience in the request",
            )
            continue
        bullets = validator._bullets("experience", entry.bullets, entry.experience_id)
        if bullets:
            kept_experience.append(ExperienceEntry(experience_id=entry.experience_id, bullets=bullets))

    kept_projects: List[ProjectEntry] = []
    for entry in document.projects:
        if entry.project_id not in known_projects:
            validator._drop(
                "projects",
                entry.project_id,
                [entry.project_id],
                UNKNOWN_EVIDENCE_ID,
                "no such project in the request",
            )
            continue
        bullets = validator._bullets("projects", entry.bullets, entry.project_id)
        if bullets:
            kept_projects.append(ProjectEntry(project_id=entry.project_id, bullets=bullets))

    cleaned = ResumeDocument(
        summary=kept_summary,
        skills=validator._skills(list(document.skills)),
        experience=kept_experience,
        projects=kept_projects,
        education=validator._education(document.education),
    )
    return cleaned, validator.dropped
