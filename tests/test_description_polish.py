"""The description-polish stage. No test here makes a network call — the SDK
client is replaced with a fake returning canned `ParsedMessage`-shaped objects,
so these assert our contract with the API and our guards, not the API itself.

The guard tests are the heart of this file. This stage's output replaces the
student's own words and becomes the evidence every later resume bullet for that
item is checked against, so a guard that silently stops working would not
surface anywhere downstream.
"""

import types

import anthropic
import httpx
import pytest

from service import description_polish
from service.description_polish import (
    DescriptionPolishError,
    DescriptionPolishRejected,
    build_user_turn,
    polish_description,
)
from service.schemas import PolishedDescription, ResumeExperience, ResumeProject


class _FakeUsage:
    input_tokens = 800
    output_tokens = 120
    cache_read_input_tokens = 600


class _FakeResponse:
    def __init__(self, parsed_output, stop_reason="end_turn"):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.model = "claude-opus-5"
        self.usage = _FakeUsage()


def _install_fake_client(monkeypatch, *, response=None, error=None, capture=None):
    async def parse(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        if error is not None:
            raise error
        return response

    client = types.SimpleNamespace(messages=types.SimpleNamespace(parse=parse))
    monkeypatch.setattr(description_polish, "_get_client", lambda: client)
    return client


def _lines(*lines):
    return _FakeResponse(PolishedDescription(lines=list(lines)))


EXPERIENCE = ResumeExperience(
    id="exp_1",
    title="IT Help Desk Assistant",
    organization="Riverbend State University",
    dates="Jun 2024 – Aug 2024",
    description=(
        "worked the help desk, mostly password resets and printer stuff, "
        "also wrote a script in Python to auto-close stale tickets"
    ),
)

PROJECT = ResumeProject(
    id="proj_1",
    name="Trailhead",
    technologies="React, Postgres",
    description="built a trail-finding app for a class project, used the park service API",
)


# --- enablement ------------------------------------------------------------


async def test_disabled_without_credentials():
    assert description_polish.is_enabled() is False


async def test_enabled_with_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert description_polish.is_enabled() is True


async def test_disabled_by_kill_switch(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(description_polish, "POLISH_ENABLED", False)
    assert description_polish.is_enabled() is False


# --- happy path ------------------------------------------------------------


async def test_returns_lines_joined_by_newlines(monkeypatch):
    """The service joins, never the browser: the joined string is the evidence
    text for this item, and a second place that assembles it is a second place
    for it to be assembled differently."""
    _install_fake_client(
        monkeypatch,
        response=_lines(
            "Resolved password and printer issues at a university help desk",
            "Wrote a Python script to auto-close stale tickets",
        ),
    )
    description, warnings = await polish_description(kind="experience", item=EXPERIENCE)

    assert description == (
        "Resolved password and printer issues at a university help desk\n"
        "Wrote a Python script to auto-close stale tickets"
    )
    assert warnings == []


async def test_polishes_a_project(monkeypatch):
    _install_fake_client(
        monkeypatch,
        response=_lines("Built a trail-finding app on the park service API for a class project"),
    )
    description, _ = await polish_description(kind="project", item=PROJECT)
    assert "trail-finding" in description


async def test_empty_description_never_reaches_the_model(monkeypatch):
    """Rejected before the call: there is nothing to rewrite, and spending a
    request to be told so is worse than saying it here."""
    called = False

    async def parse(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        description_polish,
        "_get_client",
        lambda: types.SimpleNamespace(messages=types.SimpleNamespace(parse=parse)),
    )
    blank = EXPERIENCE.model_copy(update={"description": "   "})

    with pytest.raises(DescriptionPolishRejected):
        await polish_description(kind="experience", item=blank)
    assert called is False


# --- request shape ---------------------------------------------------------


async def test_request_shape(monkeypatch):
    capture = {}
    _install_fake_client(
        monkeypatch,
        response=_lines("Resolved help desk tickets and wrote a Python script"),
        capture=capture,
    )
    await polish_description(kind="experience", item=EXPERIENCE)

    assert capture["output_format"] is PolishedDescription
    assert capture["output_config"] == {"effort": description_polish.POLISH_EFFORT}
    assert capture["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert capture["system"][0]["text"] is description_polish.SYSTEM_PROMPT


async def test_user_turn_delimits_the_notes_and_carries_the_context():
    turn = build_user_turn(kind="experience", item=EXPERIENCE)
    assert "<student_notes>" in turn and "</student_notes>" in turn
    assert "<kind>experience</kind>" in turn
    # The card's own fields reach the prompt, which is what lets the rewrite say
    # who the work was for.
    assert "Riverbend State University" in turn
    assert "Jun 2024 – Aug 2024" in turn


async def test_project_context_reaches_the_prompt():
    turn = build_user_turn(kind="project", item=PROJECT)
    assert "Trailhead" in turn
    assert "React, Postgres" in turn


async def test_user_turn_is_deterministic():
    """Equal inputs must produce byte-identical prompts, or the cached prefix
    stops being reusable."""
    assert build_user_turn(kind="experience", item=EXPERIENCE) == build_user_turn(
        kind="experience", item=EXPERIENCE
    )


async def test_system_prompt_is_a_stable_constant():
    """No per-request interpolation: a formatted system prompt is a new cache
    prefix on every call, and the caching is the point."""
    assert "{" not in description_polish.SYSTEM_PROMPT
    assert "%s" not in description_polish.SYSTEM_PROMPT


async def test_system_prompt_frames_the_notes_as_untrusted():
    assert "untrusted" in description_polish.SYSTEM_PROMPT.lower()


async def test_output_schema_stays_structured_output_compatible():
    """Structured outputs reject length and numeric constraints outright, so a
    `Field(max_length=...)` added later would be a 400 on every request. Bounds
    belong in `_clean_lines`, not the schema."""
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

    walk(PolishedDescription.model_json_schema())
    assert found == []


# --- guard A: invented figures ---------------------------------------------


async def test_drops_the_line_with_an_invented_figure_and_keeps_its_sibling(monkeypatch):
    """One fabricated line does not cost the student the honest ones."""
    _install_fake_client(
        monkeypatch,
        response=_lines(
            "Wrote a Python script to auto-close stale tickets",
            "Cut ticket backlog 40% across the department",
        ),
    )
    description, warnings = await polish_description(kind="experience", item=EXPERIENCE)

    assert description == "Wrote a Python script to auto-close stale tickets"
    assert "40" not in description
    assert warnings and "figures your notes don't mention" in warnings[0]


async def test_keeps_a_figure_the_notes_support(monkeypatch):
    item = EXPERIENCE.model_copy(
        update={"description": "closed about 30 tickets a week using Python"}
    )
    _install_fake_client(
        monkeypatch, response=_lines("Closed about 30 tickets a week using Python")
    )
    description, warnings = await polish_description(kind="experience", item=item)

    assert "30" in description
    assert warnings == []


async def test_keeps_a_figure_that_only_the_dates_support(monkeypatch):
    """The context fields widen the haystack on purpose. Without `dates` in the
    source text this honest line reads as an invented figure and is dropped."""
    _install_fake_client(
        monkeypatch,
        response=_lines(
            "Resolved help desk tickets over a 2024 summer internship",
            "Wrote a Python script to auto-close stale tickets",
        ),
    )
    description, warnings = await polish_description(kind="experience", item=EXPERIENCE)

    assert "2024" in description
    assert warnings == []


async def test_all_lines_invented_rejects_and_never_echoes_the_original(monkeypatch):
    _install_fake_client(
        monkeypatch,
        response=_lines("Cut backlog 40%", "Supported 500 staff across 12 buildings"),
    )
    with pytest.raises(DescriptionPolishRejected) as excinfo:
        await polish_description(kind="experience", item=EXPERIENCE)

    assert "kept your own words" in str(excinfo.value)
    assert "password resets" not in str(excinfo.value)


# --- guard B: dropped names ------------------------------------------------


async def test_rejects_a_rewrite_that_drops_a_tool_name(monkeypatch):
    """Tighter, entirely true, and it has cost the student a verified skill.
    Nothing downstream can notice, which is why this rejects rather than
    patches."""
    _install_fake_client(
        monkeypatch,
        response=_lines("Automated ticket triage for a university help desk"),
    )
    with pytest.raises(DescriptionPolishRejected) as excinfo:
        await polish_description(kind="experience", item=EXPERIENCE)

    assert "Python" in str(excinfo.value)


async def test_rewording_an_ordinary_opening_verb_is_not_a_rejection(monkeypatch):
    """The spurious-failure guard. "Answered" opens a sentence and is not a name,
    so replacing it with "Resolved" must be allowed — a rule that rejected this
    would reject most good rewrites."""
    item = EXPERIENCE.model_copy(update={"description": "Answered support tickets"})
    _install_fake_client(monkeypatch, response=_lines("Resolved support tickets for staff"))

    description, warnings = await polish_description(kind="experience", item=item)
    assert description == "Resolved support tickets for staff"
    assert warnings == []


async def test_a_name_surviving_only_on_a_dropped_line_does_not_count(monkeypatch):
    """Guard B runs against the KEPT lines. If the only mention of Python was on
    the line guard A just deleted, the name did not survive."""
    _install_fake_client(
        monkeypatch,
        response=_lines(
            "Resolved password and printer issues at a university help desk",
            "Wrote a Python script that closed 400 stale tickets",
        ),
    )
    with pytest.raises(DescriptionPolishRejected) as excinfo:
        await polish_description(kind="experience", item=EXPERIENCE)

    assert "Python" in str(excinfo.value)


# --- output sanitization ---------------------------------------------------


async def test_strips_bullet_glyphs_and_enumeration(monkeypatch):
    _install_fake_client(
        monkeypatch,
        response=_lines("• Resolved help desk tickets", "2) Wrote a Python script"),
    )
    description, _ = await polish_description(kind="experience", item=EXPERIENCE)

    assert description == "Resolved help desk tickets\nWrote a Python script"


async def test_drops_blank_lines_and_bounds_the_count(monkeypatch):
    _install_fake_client(
        monkeypatch,
        response=_lines(
            "Resolved help desk tickets",
            "   ",
            "Wrote a Python script",
            "Trained student staff",
            "Documented the runbook",
            "Audited the printer queue",
        ),
    )
    description, _ = await polish_description(kind="experience", item=EXPERIENCE)

    lines = description.split("\n")
    assert "" not in lines
    assert len(lines) == description_polish.MAX_POLISH_LINES


async def test_empty_lines_list_is_a_rejection_with_advice(monkeypatch):
    """The prompt asks for an empty list when the notes are too thin, so this is
    a real answer rather than a failure — and only the student can fix it."""
    _install_fake_client(monkeypatch, response=_lines())

    with pytest.raises(DescriptionPolishRejected) as excinfo:
        await polish_description(kind="experience", item=EXPERIENCE)
    assert "enough detail" in str(excinfo.value).lower()


# --- failure modes ---------------------------------------------------------


async def test_refusal_raises(monkeypatch):
    _install_fake_client(
        monkeypatch,
        response=_FakeResponse(PolishedDescription(lines=["x"]), stop_reason="refusal"),
    )
    with pytest.raises(DescriptionPolishError):
        await polish_description(kind="experience", item=EXPERIENCE)


async def test_max_tokens_raises(monkeypatch):
    _install_fake_client(
        monkeypatch,
        response=_FakeResponse(PolishedDescription(lines=["x"]), stop_reason="max_tokens"),
    )
    with pytest.raises(DescriptionPolishError):
        await polish_description(kind="experience", item=EXPERIENCE)


async def test_missing_structured_output_raises(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(None))
    with pytest.raises(DescriptionPolishError):
        await polish_description(kind="experience", item=EXPERIENCE)


async def test_api_status_error_reports_status_only(monkeypatch):
    """Status and type reach the log; the request body must not."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.RateLimitError(
        "rate limited",
        response=httpx.Response(429, request=request, json={"error": {"message": "SENSITIVE"}}),
        body=None,
    )
    _install_fake_client(monkeypatch, error=error)

    with pytest.raises(DescriptionPolishError) as excinfo:
        await polish_description(kind="experience", item=EXPERIENCE)

    assert "429" in str(excinfo.value)
    assert "SENSITIVE" not in str(excinfo.value)


async def test_connection_error_raises(monkeypatch):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    _install_fake_client(monkeypatch, error=anthropic.APIConnectionError(request=request))

    with pytest.raises(DescriptionPolishError):
        await polish_description(kind="experience", item=EXPERIENCE)


async def test_unexpected_error_is_caught_defensively(monkeypatch):
    """This stage must never 500 the request, whatever the SDK does."""
    _install_fake_client(monkeypatch, error=ValueError("SENSITIVE internal detail"))

    with pytest.raises(DescriptionPolishError) as excinfo:
        await polish_description(kind="experience", item=EXPERIENCE)

    assert "ValueError" in str(excinfo.value)
    assert "SENSITIVE" not in str(excinfo.value)
