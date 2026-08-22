"""Claude API resume-drafting stage. No test here makes a network call — the SDK
client is replaced with a fake returning canned `ParsedMessage`-shaped objects,
so these assert our contract with the API, not the API itself.

Model output is not deterministic, so nothing here asserts on generated prose.
What is asserted is the request shape, the failure modes, and the fact that the
evidence contract runs before anything reaches the caller.
"""

import types

import anthropic
import httpx
import pytest

from service import resume_generation
from service.market_matching import MarketProfile, MarketSkill
from service.resume_evidence import run_match
from service.resume_generation import (
    ResumeGenerationError,
    build_user_turn,
    generate_resume,
)
from service.schemas import (
    AcademicProfile,
    Claim,
    Coursework,
    EducationBlock,
    ExperienceEntry,
    ResumeDocument,
    ResumeExperience,
    ResumeProject,
    ResumeTargetRole,
    SkillEntry,
)


class _FakeUsage:
    input_tokens = 2400
    output_tokens = 900
    cache_read_input_tokens = 1800


class _FakeResponse:
    def __init__(self, parsed_output, stop_reason="end_turn"):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.model = "claude-opus-5"
        self.usage = _FakeUsage()


def _install_fake_client(monkeypatch, *, response=None, error=None, capture=None):
    # `async def`: the SDK client is AsyncAnthropic, because the same module runs
    # on Cloudflare Workers where only async HTTP clients exist.
    async def parse(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        if error is not None:
            raise error
        return response

    client = types.SimpleNamespace(messages=types.SimpleNamespace(parse=parse))
    monkeypatch.setattr(resume_generation, "_get_client", lambda: client)
    return client


CAREER = ResumeTargetRole(role="Backend Engineer", level="Entry level")

PROFILE = AcademicProfile(
    institution="Riverbend State University",
    degree="Bachelor of Science",
    major="Computer Science",
    expected_graduation_date="May 2027",
    coursework=[Coursework(id="c1", course_code="CS 310", course_name="Data Structures")],
    skills=["Python"],
)

EXPERIENCE = [ResumeExperience(id="exp_1", description="Answered student support tickets.")]
PROJECTS = [ResumeProject(id="proj_1", technologies="Python", description="Built a course planner.")]

MARKET = MarketProfile(
    skills=[
        MarketSkill(name="Python", posting_count=800, frequency=0.80),
        MarketSkill(name="Kubernetes", posting_count=600, frequency=0.60),
    ]
)

SAMPLE = ResumeDocument(
    summary=Claim(text="Computer Science student seeking a backend role.", evidence=["edu_major"]),
    skills=[SkillEntry(name="Python", evidence=["skill_0"], market_skill_id="python")],
    experience=[
        ExperienceEntry(
            experience_id="exp_1",
            bullets=[Claim(text="Answered student support tickets.", evidence=["exp_1"])],
        )
    ],
    education=EducationBlock(institution="Riverbend State University"),
)


async def _generate(**overrides):
    kwargs = dict(
        career=CAREER,
        profile=PROFILE,
        experience=EXPERIENCE,
        projects=PROJECTS,
        market_profile=MARKET,
        market_match=run_match(PROFILE, MARKET, EXPERIENCE, PROJECTS),
        variant=0,
    )
    kwargs.update(overrides)
    return await generate_resume(**kwargs)


# --- enablement ------------------------------------------------------------


async def test_disabled_without_credentials():
    assert resume_generation.is_enabled() is False


async def test_enabled_with_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert resume_generation.is_enabled() is True


async def test_disabled_by_kill_switch(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(resume_generation, "RESUME_GENERATION_ENABLED", False)
    assert resume_generation.is_enabled() is False


# --- request shape ---------------------------------------------------------


async def test_request_shape(monkeypatch):
    """The system prompt must be cacheable, the profile must be delimited and
    framed as data, and the structured-output class must be the one the
    validator understands."""
    capture = {}
    _install_fake_client(monkeypatch, response=_FakeResponse(SAMPLE), capture=capture)
    await _generate()

    assert capture["output_format"] is ResumeDocument
    assert capture["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert capture["output_config"] == {"effort": resume_generation.RESUME_EFFORT}

    user_turn = capture["messages"][0]["content"]
    assert "<student_profile_items>" in user_turn
    assert "<market_demand>" in user_turn
    assert "untrusted" in resume_generation.SYSTEM_PROMPT.lower()


async def test_user_turn_exposes_every_citable_id():
    """The model can only cite ids it was shown. If an id the validator accepts
    never reaches the prompt, that item is silently unusable."""
    turn = build_user_turn(
        career=CAREER,
        profile=PROFILE,
        experience=EXPERIENCE,
        projects=PROJECTS,
        market_profile=MARKET,
        market_match=run_match(PROFILE, MARKET, EXPERIENCE, PROJECTS),
        variant=0,
    )
    for item_id in ("c1", "skill_0", "exp_1", "proj_1", "edu_major"):
        assert item_id in turn, item_id


async def test_user_turn_is_deterministic():
    """Byte-identical prompts for equal inputs: the cached prefix depends on it,
    and so does being able to assert on payloads at all."""
    args = dict(
        career=CAREER,
        profile=PROFILE,
        experience=EXPERIENCE,
        projects=PROJECTS,
        market_profile=MARKET,
        market_match=run_match(PROFILE, MARKET, EXPERIENCE, PROJECTS),
        variant=0,
    )
    assert build_user_turn(**args) == build_user_turn(**args)


async def test_variant_reaches_the_prompt():
    turn = build_user_turn(
        career=CAREER,
        profile=PROFILE,
        experience=EXPERIENCE,
        projects=PROJECTS,
        market_profile=MARKET,
        market_match=run_match(PROFILE, MARKET, EXPERIENCE, PROJECTS),
        variant=3,
    )
    assert "<variant>3</variant>" in turn


async def test_market_demand_carries_match_status_not_just_demand():
    """Demand alone would invite the model to treat popularity as evidence. Each
    market skill has to arrive with the matcher's verdict attached."""
    turn = build_user_turn(
        career=CAREER,
        profile=PROFILE,
        experience=EXPERIENCE,
        projects=PROJECTS,
        market_profile=MARKET,
        market_match=run_match(PROFILE, MARKET, EXPERIENCE, PROJECTS),
        variant=0,
    )
    assert '"status"' in turn
    assert "not_verified" in turn  # Kubernetes has no support in this profile


async def test_system_prompt_is_a_stable_constant():
    """No per-request interpolation: a formatted system prompt is a new cache
    prefix on every call, and the caching is the point."""
    assert "{" not in resume_generation.SYSTEM_PROMPT
    assert "%s" not in resume_generation.SYSTEM_PROMPT


async def test_output_schema_stays_structured_output_compatible():
    """Structured outputs reject length and numeric constraints outright. A
    `Field(max_length=...)` added to a resume model later would be a 400 on
    every request, and the natural place to discover that is here rather than
    in production — bounds belong in the validator, not the schema.
    """
    banned = {
        "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum",
        "exclusiveMaximum", "multipleOf", "minItems", "maxItems", "pattern",
    }
    found = []

    def walk(node, path="#"):
        if isinstance(node, dict):
            found.extend(f"{path}.{k}" for k in node if k in banned)
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(ResumeDocument.model_json_schema())
    assert found == []


async def test_max_tokens_leaves_room_for_thinking_and_a_full_resume():
    """This model thinks by default and max_tokens caps thinking plus response
    together, so a tight cap surfaces as stop_reason=max_tokens and a failed
    request rather than a shorter resume."""
    assert resume_generation.RESUME_MAX_TOKENS >= 16000


# --- the evidence contract runs before anything is returned ----------------


async def test_validation_runs_on_model_output(monkeypatch):
    """The guardrail is not optional or caller-invoked: a fabricated bullet must
    not survive a normal call to generate_resume."""
    fabricated = SAMPLE.model_copy(
        update={
            "experience": [
                ExperienceEntry(
                    experience_id="exp_1",
                    bullets=[
                        Claim(text="Resolved 500+ tickets, cutting wait times 40%.", evidence=["exp_1"])
                    ],
                )
            ]
        }
    )
    _install_fake_client(monkeypatch, response=_FakeResponse(fabricated))
    document, dropped, warnings = await _generate()

    assert document.experience == []
    assert any(d.reason == "unsupported_number" for d in dropped)
    assert any("could not be traced" in w for w in warnings)


async def test_gap_skill_never_survives_a_generate_call(monkeypatch):
    leaked = SAMPLE.model_copy(
        update={"skills": [SkillEntry(name="Kubernetes", evidence=["skill_0"])]}
    )
    _install_fake_client(monkeypatch, response=_FakeResponse(leaked))
    document, dropped, _ = await _generate()

    assert "Kubernetes" not in [s.name for s in document.skills]
    assert dropped


async def test_clean_output_passes_through_without_warnings(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(SAMPLE))
    document, dropped, warnings = await _generate()

    assert dropped == []
    assert warnings == []
    assert document.summary.text


# --- failure modes: every one must raise ResumeGenerationError -------------


async def test_refusal_raises(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(None, stop_reason="refusal"))
    with pytest.raises(ResumeGenerationError):
        await _generate()


async def test_truncated_output_raises(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(None, stop_reason="max_tokens"))
    with pytest.raises(ResumeGenerationError):
        await _generate()


async def test_missing_parsed_output_raises(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(None))
    with pytest.raises(ResumeGenerationError):
        await _generate()


async def test_api_status_error_raises_without_leaking_profile_text(monkeypatch):
    """The exception message reaches the logs, so it carries the status code and
    nothing about the student."""
    api_error = anthropic.RateLimitError(
        "rate limited",
        response=httpx.Response(
            429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
        body=None,
    )
    _install_fake_client(monkeypatch, error=api_error)

    secret = AcademicProfile(institution="CONFIDENTIAL ACADEMY", skills=["Python"])
    with pytest.raises(ResumeGenerationError) as excinfo:
        await _generate(profile=secret, market_match=run_match(secret, MARKET))

    assert "CONFIDENTIAL" not in str(excinfo.value)
    assert "429" in str(excinfo.value)


async def test_connection_error_raises(monkeypatch):
    _install_fake_client(
        monkeypatch,
        error=anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com")
        ),
    )
    with pytest.raises(ResumeGenerationError):
        await _generate()


async def test_unexpected_error_is_contained(monkeypatch):
    _install_fake_client(monkeypatch, error=ValueError("boom"))
    with pytest.raises(ResumeGenerationError):
        await _generate()
