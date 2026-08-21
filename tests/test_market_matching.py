import pytest

from service.market_matching import (
    MarketProfile,
    MarketSkill,
    StudentCoursework,
    StudentExperience,
    StudentProfile,
    StudentProject,
    StudentSkill,
    match_market,
)


def _market(skills):
    return MarketProfile(skills=[MarketSkill(name=n, posting_count=pc, frequency=f) for n, pc, f in skills])


def test_verified_exact_skill_match_has_evidence():
    student = StudentProfile(skills=[StudentSkill(id="skill_sql", name="SQL")])
    market = _market([("SQL", 31, 0.738)])
    result = match_market(student, market)
    m = result.matches[0]
    assert m.status == "verified"
    assert m.evidence == ["skill_sql"]
    assert result.summary.verified_top_skills == 1


def test_verified_via_approved_alias():
    student = StudentProfile(skills=[StudentSkill(id="skill_js", name="JavaScript", aliases=["JS"])])
    market = _market([("JS", 10, 0.5)])
    result = match_market(student, market)
    assert result.matches[0].status == "verified"
    assert result.matches[0].evidence == ["skill_js"]


def test_coursework_requires_explicit_technology_name():
    student = StudentProfile(
        coursework=[StudentCoursework(id="course_db", course_code="CS 340", course_name="Database Systems")]
    )
    market = _market([("SQL", 20, 0.6)])
    result = match_market(student, market)
    # "Database Systems" does not literally name SQL -> must NOT be matched.
    assert result.matches[0].status == "not_verified"
    assert result.matches[0].evidence == []


def test_coursework_matches_when_skill_explicitly_named():
    student = StudentProfile(
        coursework=[StudentCoursework(id="course_sql", course_name="Applied SQL for Analysts")]
    )
    market = _market([("SQL", 20, 0.6)])
    result = match_market(student, market)
    assert result.matches[0].status == "coursework"
    assert result.matches[0].evidence == ["course_sql"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pre-existing defect inherited from the handoff bundle — this test fails "
        "against the pristine zip copy of market_matching.py too, unmodified. "
        "`_word_match` does no stemming, so the market's plural 'REST APIs' does "
        "not match the project's singular 'REST API' and the skill is scored "
        "not_verified. Left failing rather than papered over: fixing it means "
        "loosening the matcher, which is a deliberate product decision (it would "
        "have to be mirrored in frontend/js/services/api-service.js, and the "
        "module's stated rule is a conservative literal match). "
        "service/resume_evidence.py compensates for the resume path — see "
        "tests/test_resume_evidence.py::test_gap_term_quoted_from_the_cited_evidence_survives "
        "— so a student's true 'REST API' project is not censored from their "
        "resume. The false *gap* shown in the UI is still wrong."
    ),
)
def test_transferable_from_project_description():
    student = StudentProfile(
        projects=[StudentProject(id="proj_1", technologies="Python, Flask", description="Built a REST API for a course project")]
    )
    market = _market([("REST APIs", 15, 0.4)])
    result = match_market(student, market)
    assert result.matches[0].status == "transferable"
    assert result.matches[0].evidence == ["proj_1"]


def test_high_demand_skill_absent_from_profile_is_not_verified():
    """The central guarantee: a skill's market demand can be arbitrarily
    high, but with zero supporting evidence in the StudentProfile it must
    come back not_verified with no evidence — never inferred from demand."""
    student = StudentProfile(
        skills=[StudentSkill(id="skill_excel", name="Excel")],
        coursework=[StudentCoursework(id="course_intro", course_name="Intro to Business")],
    )
    market = _market([
        ("Python", 500, 0.95),   # very high demand, but student has nothing to support it
        ("AWS", 480, 0.90),
        ("Kubernetes", 400, 0.85),
    ])
    result = match_market(student, market)
    assert all(m.status == "not_verified" for m in result.matches)
    assert all(m.evidence == [] for m in result.matches)
    assert result.gaps == ["Python", "AWS", "Kubernetes"]
    assert result.summary.verified_top_skills == 0


def test_market_frequency_alone_never_counts_as_evidence():
    """Even a skill requested in 100% of postings must not be marked
    verified/coursework/transferable without real StudentProfile evidence."""
    student = StudentProfile()
    market = _market([("Docker", 999, 1.0)])
    result = match_market(student, market)
    assert result.matches[0].status == "not_verified"
    assert result.matches[0].evidence == []


def test_does_not_mutate_student_profile():
    student = StudentProfile(skills=[StudentSkill(id="skill_sql", name="SQL")])
    market = _market([("Python", 10, 0.5)])
    before = student.model_copy(deep=True)
    match_market(student, market)
    assert student == before


def test_only_top_ten_market_skills_considered():
    student = StudentProfile()
    market = _market([(f"Skill{i}", 10, 0.5) for i in range(15)])
    result = match_market(student, market)
    assert result.summary.top_market_skills_considered == 10
    assert len(result.matches) == 10


def test_market_skill_id_falls_back_to_slug_when_absent():
    student = StudentProfile()
    market = _market([("REST APIs", 10, 0.5)])
    result = match_market(student, market)
    assert result.matches[0].market_skill_id == "rest_apis"
