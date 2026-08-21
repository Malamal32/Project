"""The evidence contract. These are the tests that decide whether a fabricated
line can reach a student, so they are written as attacks: each one hands the
validator a plausible, well-phrased claim that is not supported by the profile
and asserts it does not survive.

No model, no network — `validate_document` is pure, and every "model output"
here is a hand-built ResumeDocument.
"""

import pytest

from service.market_matching import MarketProfile, MarketSkill
from service.resume_evidence import (
    DUPLICATE,
    EVIDENCE_OUT_OF_SCOPE,
    GAP_SKILL_MENTIONED,
    NO_EVIDENCE,
    NOT_VERIFIED_SKILL,
    UNKNOWN_EVIDENCE_ID,
    UNSUPPORTED_NUMBER,
    VERBATIM_MISMATCH,
    collect_evidence_ids,
    run_match,
    validate_document,
)
from service.schemas import (
    AcademicProfile,
    Claim,
    Coursework,
    EducationBlock,
    ExperienceEntry,
    ProjectEntry,
    ResumeDocument,
    ResumeExperience,
    ResumeProject,
    SkillEntry,
)

# --- fixtures --------------------------------------------------------------

PROFILE = AcademicProfile(
    institution="Riverbend State University",
    degree="Bachelor of Science",
    degree_level="Bachelor's",
    major="Computer Science",
    expected_graduation_date="May 2027",
    gpa="3.72/4.00",
    coursework=[
        Coursework(id="c1", course_code="CS 310", course_name="Data Structures", credits=3, grade="A"),
        Coursework(id="c2", course_code="CS 340", course_name="Database Systems", credits=3, grade="A-"),
    ],
    skills=["Python", "Git"],
    certifications=["AWS Cloud Practitioner"],
    honors=["Dean's List, Fall 2025"],
)

EXPERIENCE = [
    ResumeExperience(
        id="exp_1",
        title="Student Assistant",
        organization="University IT Help Desk",
        description="Answered student support tickets and reset accounts.",
    )
]

PROJECTS = [
    ResumeProject(
        id="proj_1",
        name="Course Planner",
        technologies="Python, Flask",
        description="Built a web app for planning course schedules.",
    )
]

# "Kubernetes" is in demand and appears nowhere in the profile -> a gap.
MARKET = MarketProfile(
    skills=[
        MarketSkill(name="Python", posting_count=800, frequency=0.80),
        MarketSkill(name="Kubernetes", posting_count=600, frequency=0.60),
    ]
)


def _match():
    return run_match(PROFILE, MARKET, EXPERIENCE, PROJECTS)


def _document(**overrides):
    """A document that fully validates, so each test can break exactly one thing."""
    base = dict(
        summary=Claim(text="Computer Science student seeking a backend role.", evidence=["edu_major"]),
        skills=[SkillEntry(name="Python", evidence=["skill_0"], market_skill_id="python")],
        experience=[
            ExperienceEntry(
                experience_id="exp_1",
                bullets=[Claim(text="Answered student support tickets and reset accounts.", evidence=["exp_1"])],
            )
        ],
        projects=[
            ProjectEntry(
                project_id="proj_1",
                bullets=[Claim(text="Built a web app for planning course schedules.", evidence=["proj_1"])],
            )
        ],
        education=EducationBlock(
            institution="Riverbend State University",
            degree_line="Bachelor of Science",
            graduation_date="May 2027",
            gpa="3.72/4.00",
            coursework=[Claim(text="CS 340 Database Systems", evidence=["c2"])],
        ),
    )
    base.update(overrides)
    return ResumeDocument(**base)


def _validate(document):
    return validate_document(document, PROFILE, _match(), EXPERIENCE, PROJECTS)


def _reasons(dropped):
    return {d.reason for d in dropped}


# --- the citable set -------------------------------------------------------


def test_evidence_ids_cover_every_profile_item():
    ids = collect_evidence_ids(PROFILE, EXPERIENCE, PROJECTS)
    for expected in ("c1", "c2", "skill_0", "skill_1", "cert_0", "honor_0", "exp_1", "proj_1"):
        assert expected in ids, expected
    # Education fields are citable so a summary can name the school or major.
    assert "edu_institution" in ids and "edu_major" in ids


def test_matcher_and_validator_agree_on_ids():
    """The load-bearing invariant: the ids the matcher hands the model must be
    the ids the validator can resolve. If these drift, every skill claim is
    dropped as an unknown id and the failure looks like a model problem."""
    known = set(collect_evidence_ids(PROFILE, EXPERIENCE, PROJECTS))
    for match in _match().matches:
        assert set(match.evidence).issubset(known), match


# --- the happy path must actually pass -------------------------------------


def test_supported_document_survives_intact():
    document, dropped = _validate(_document())
    assert dropped == []
    assert document.summary.text
    assert [s.name for s in document.skills] == ["Python"]
    assert len(document.experience[0].bullets) == 1
    assert document.education.gpa == "3.72/4.00"


# --- fabrication: the id checks --------------------------------------------


def test_bullet_citing_an_unknown_id_is_dropped():
    document, dropped = _validate(
        _document(
            experience=[
                ExperienceEntry(
                    experience_id="exp_1",
                    bullets=[
                        Claim(text="Answered student support tickets and reset accounts.", evidence=["exp_1"]),
                        Claim(text="Led migration of the ticketing platform.", evidence=["exp_999"]),
                    ],
                )
            ]
        )
    )
    assert UNKNOWN_EVIDENCE_ID in _reasons(dropped)
    # The legitimate bullet beside it is untouched.
    assert len(document.experience[0].bullets) == 1


def test_bullet_citing_the_wrong_experience_is_dropped():
    """A legal id is not enough — a bullet has to cite the item it describes,
    or it could narrate job A while pointing at job B."""
    document, dropped = _validate(
        _document(
            experience=[
                ExperienceEntry(
                    experience_id="exp_1",
                    bullets=[Claim(text="Built a web app for planning schedules.", evidence=["proj_1"])],
                )
            ]
        )
    )
    assert EVIDENCE_OUT_OF_SCOPE in _reasons(dropped)
    assert document.experience == []


def test_entry_for_an_experience_that_was_never_submitted_is_dropped():
    document, dropped = _validate(
        _document(
            experience=[
                ExperienceEntry(
                    experience_id="exp_invented",
                    bullets=[Claim(text="Managed a team of engineers.", evidence=["exp_invented"])],
                )
            ]
        )
    )
    assert UNKNOWN_EVIDENCE_ID in _reasons(dropped)
    assert document.experience == []


def test_claim_with_no_evidence_is_dropped():
    document, dropped = _validate(_document(summary=Claim(text="A strong candidate.", evidence=[])))
    assert NO_EVIDENCE in _reasons(dropped)
    assert document.summary.text == ""


# --- fabrication: invented numbers -----------------------------------------


def test_invented_metric_is_dropped():
    """The highest-value check. The bullet correctly cites the experience it
    embellished, so every id resolves — only the number gives it away."""
    document, dropped = _validate(
        _document(
            experience=[
                ExperienceEntry(
                    experience_id="exp_1",
                    bullets=[
                        Claim(text="Resolved 500+ support tickets, cutting wait times by 40%.", evidence=["exp_1"])
                    ],
                )
            ]
        )
    )
    assert UNSUPPORTED_NUMBER in _reasons(dropped)
    assert document.experience == []


def test_same_bullet_without_the_invented_number_survives():
    document, dropped = _validate(
        _document(
            experience=[
                ExperienceEntry(
                    experience_id="exp_1",
                    bullets=[Claim(text="Resolved support tickets and cut wait times.", evidence=["exp_1"])],
                )
            ]
        )
    )
    assert dropped == []
    assert len(document.experience[0].bullets) == 1


def test_number_present_in_the_evidence_is_allowed():
    """Not every figure is a fabrication — a GPA or a credit count that the
    profile actually states must survive, or the check is unusable."""
    document, dropped = _validate(
        _document(summary=Claim(text="Computer Science student with a 3.72/4.00 GPA.", evidence=["edu_gpa"]))
    )
    assert dropped == []
    assert "3.72" in document.summary.text


# --- gap skills: the two independent gates ---------------------------------


def test_gap_skill_is_rejected_from_the_skills_list():
    document, dropped = _validate(
        _document(
            skills=[
                SkillEntry(name="Python", evidence=["skill_0"]),
                SkillEntry(name="Kubernetes", evidence=["c1"]),
            ]
        )
    )
    assert {GAP_SKILL_MENTIONED, NOT_VERIFIED_SKILL} & _reasons(dropped)
    assert "Kubernetes" not in [s.name for s in document.skills]


def test_gap_skill_smuggled_into_the_summary_is_dropped():
    """The second gate. The skills-list check only sees the skills array, so a
    gap skill mentioned in prose needs its own check."""
    document, dropped = _validate(
        _document(
            summary=Claim(
                text="Computer Science student with exposure to Kubernetes.", evidence=["edu_major"]
            )
        )
    )
    assert GAP_SKILL_MENTIONED in _reasons(dropped)
    assert "Kubernetes" not in document.summary.text


def test_gap_skill_in_a_bullet_is_dropped():
    document, dropped = _validate(
        _document(
            projects=[
                ProjectEntry(
                    project_id="proj_1",
                    bullets=[Claim(text="Deployed the planner on Kubernetes.", evidence=["proj_1"])],
                )
            ]
        )
    )
    assert GAP_SKILL_MENTIONED in _reasons(dropped)
    assert document.projects == []


def test_gap_term_quoted_from_the_cited_evidence_survives():
    """The matcher does no stemming, so it scores "REST APIs" a gap for a student
    whose project literally says "Built a REST API". The gap guard must not turn
    that into censorship of the student's own record — a claim whose evidence
    contains the term is quoting, not inventing.

    See tests/test_market_matching.py::test_transferable_from_project_description
    for the underlying matcher behaviour this compensates for.
    """
    profile = AcademicProfile(major="Computer Science", skills=["Python"])
    projects = [
        ResumeProject(id="proj_1", technologies="Python, Flask", description="Built a REST API for a course project")
    ]
    market = MarketProfile(skills=[MarketSkill(name="REST APIs", posting_count=15, frequency=0.4)])
    match = run_match(profile, market, (), projects)
    assert match.matches[0].status == "not_verified"  # the matcher's literal verdict

    document = ResumeDocument(
        summary=Claim(text="Computer Science student.", evidence=["edu_major"]),
        projects=[
            ProjectEntry(
                project_id="proj_1",
                bullets=[Claim(text="Built a REST API for a course project.", evidence=["proj_1"])],
            )
        ],
    )
    cleaned, dropped = validate_document(document, profile, match, (), projects)

    assert dropped == []
    assert "REST API" in cleaned.projects[0].bullets[0].text


def test_gap_term_absent_from_the_cited_evidence_is_still_dropped():
    """The escape hatch above must not become a hole: a gap term the evidence
    never mentions is exactly what the guard exists to catch."""
    document, dropped = _validate(
        _document(
            projects=[
                ProjectEntry(
                    project_id="proj_1",
                    bullets=[Claim(text="Deployed the planner on Kubernetes.", evidence=["proj_1"])],
                )
            ]
        )
    )
    assert GAP_SKILL_MENTIONED in _reasons(dropped)
    assert document.projects == []


def test_skill_the_student_never_listed_is_dropped():
    """Not a market gap and not a claimed skill — the model simply decided the
    student knows it. "Database Systems" as a course does not make SQL a skill."""
    document, dropped = _validate(_document(skills=[SkillEntry(name="SQL", evidence=["c2"])]))
    assert NOT_VERIFIED_SKILL in _reasons(dropped)
    assert document.skills == []


# --- verbatim echo ---------------------------------------------------------


def test_reworded_coursework_is_dropped():
    document, dropped = _validate(
        _document(
            education=EducationBlock(
                coursework=[Claim(text="Advanced Database Systems & Design", evidence=["c2"])]
            )
        )
    )
    assert VERBATIM_MISMATCH in _reasons(dropped)
    assert document.education.coursework == []


def test_reformatted_graduation_date_is_nulled():
    """"2027-05" is the same date and still a changed fact — the student wrote
    "May 2027" and that is what the resume says."""
    document, dropped = _validate(
        _document(
            education=EducationBlock(institution="Riverbend State University", graduation_date="2027-05")
        )
    )
    assert VERBATIM_MISMATCH in _reasons(dropped)
    assert document.education.graduation_date is None
    assert document.education.institution == "Riverbend State University"


def test_invented_gpa_is_nulled():
    document, dropped = _validate(_document(education=EducationBlock(gpa="3.9/4.00")))
    assert VERBATIM_MISMATCH in _reasons(dropped)
    assert document.education.gpa is None


# --- duplicates ------------------------------------------------------------


def test_repeated_skill_is_dropped_once():
    document, dropped = _validate(
        _document(
            skills=[
                SkillEntry(name="Python", evidence=["skill_0"]),
                SkillEntry(name="python", evidence=["skill_0"]),
            ]
        )
    )
    assert DUPLICATE in _reasons(dropped)
    assert len(document.skills) == 1


# --- contract-level behaviour ----------------------------------------------


def test_input_document_is_never_mutated():
    document = _document(summary=Claim(text="Fabricated.", evidence=["nope"]))
    before = document.model_dump()
    _validate(document)
    assert document.model_dump() == before


def test_everything_invalid_still_returns_a_document():
    """A resume that loses every line is a bad resume, not a failed request —
    the student gets an empty draft plus the reasons, never an exception."""
    document, dropped = _validate(
        _document(
            summary=Claim(text="Seasoned engineer.", evidence=["ghost"]),
            skills=[SkillEntry(name="Kubernetes", evidence=["ghost"])],
            experience=[
                ExperienceEntry(experience_id="ghost", bullets=[Claim(text="Led a team.", evidence=["ghost"])])
            ],
            projects=[],
            education=EducationBlock(gpa="4.0"),
        )
    )
    assert isinstance(document, ResumeDocument)
    assert document.summary.text == ""
    assert document.skills == [] and document.experience == []
    assert len(dropped) >= 3


def test_dropped_claims_carry_a_reason_and_the_text():
    _, dropped = _validate(_document(summary=Claim(text="Invented claim.", evidence=["ghost"])))
    entry = next(d for d in dropped if d.reason == UNKNOWN_EVIDENCE_ID)
    assert entry.section == "summary"
    assert entry.text == "Invented claim."
    assert "ghost" in entry.detail
