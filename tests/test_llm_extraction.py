"""Claude API extraction stage. No test here makes a network call — the SDK
client is replaced with a fake that returns canned `ParsedMessage`-shaped
objects, so these assert our contract with the API, not the API itself."""

import types

import anthropic
import httpx
import pytest

from service import llm_extraction
from service.llm_extraction import (
    ExtractedCourse,
    ExtractedProfile,
    LlmExtractionError,
    extract_academic_profile,
)


class _FakeUsage:
    input_tokens = 1200
    output_tokens = 300
    cache_read_input_tokens = 900


class _FakeResponse:
    def __init__(self, parsed_output, stop_reason="end_turn"):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.model = "claude-opus-5"
        self.usage = _FakeUsage()


def _install_fake_client(monkeypatch, *, response=None, error=None, capture=None):
    def parse(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        if error is not None:
            raise error
        return response

    client = types.SimpleNamespace(messages=types.SimpleNamespace(parse=parse))
    monkeypatch.setattr(llm_extraction, "_get_client", lambda: client)
    return client


SAMPLE = ExtractedProfile(
    institution="Riverbend State University",
    degree="Bachelor of Science",
    degree_level="Bachelor's",
    major="Computer Science",
    gpa="3.72/4.00",
    expected_graduation_date="May 2027",
    coursework=[
        ExtractedCourse(course_code="CS 310", course_name="Data Structures", credits=3, grade="A", term="Fall 2025"),
        ExtractedCourse(course_code="CS 340", course_name="Database Systems", credits=3, grade="A-"),
    ],
    honors=["Dean's List, Fall 2025"],
)


# --- enablement ------------------------------------------------------------


def test_disabled_without_credentials():
    assert llm_extraction.is_enabled() is False


def test_enabled_with_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert llm_extraction.is_enabled() is True


def test_disabled_by_kill_switch(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(llm_extraction, "LLM_EXTRACTION_ENABLED", False)
    assert llm_extraction.is_enabled() is False


# --- happy path ------------------------------------------------------------


def test_maps_model_output_to_academic_profile(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(SAMPLE))

    profile, warnings = extract_academic_profile("some transcript text")

    assert profile.institution == "Riverbend State University"
    assert profile.degree_level == "Bachelor's"
    assert profile.gpa == "3.72/4.00"
    assert len(profile.coursework) == 2
    assert profile.coursework[0].course_code == "CS 310"
    assert profile.coursework[0].credits == 3.0
    assert profile.coursework[1].term is None
    assert warnings == []


def test_ids_are_server_assigned_and_nothing_is_pre_approved(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(SAMPLE))

    profile, _ = extract_academic_profile("text")

    ids = [c.id for c in profile.coursework]
    assert all(ids) and len(set(ids)) == len(ids)
    assert all(c.student_approved is False for c in profile.coursework)


def test_missing_fields_produce_warnings_not_values(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(ExtractedProfile()))

    profile, warnings = extract_academic_profile("text")

    assert profile.institution is None
    assert profile.degree is None
    assert profile.coursework == []
    assert len(warnings) == 4


def test_request_shape(monkeypatch):
    """The system prompt must be cacheable and the document must be delimited
    and clearly framed as data."""
    capture = {}
    _install_fake_client(monkeypatch, response=_FakeResponse(SAMPLE), capture=capture)

    extract_academic_profile("TRANSCRIPT BODY")

    assert capture["output_format"] is ExtractedProfile
    assert capture["system"][0]["cache_control"] == {"type": "ephemeral"}
    user_content = capture["messages"][0]["content"]
    assert "<student_document>" in user_content
    assert "TRANSCRIPT BODY" in user_content
    assert "untrusted" in llm_extraction.SYSTEM_PROMPT.lower()


def test_max_tokens_leaves_room_for_thinking_and_a_long_course_list():
    """This model thinks by default and `max_tokens` caps thinking plus response
    together, so a tight cap shows up as stop_reason=max_tokens and a silent
    downgrade to regex extraction rather than as an error."""
    assert llm_extraction.LLM_MAX_TOKENS >= 32000


# --- sanitization of model output -----------------------------------------


def test_sanitizes_and_bounds_model_output(monkeypatch):
    noisy = ExtractedProfile(
        institution="  Riverbend\x00   State\n University  ",
        major="X" * 5000,
        coursework=[
            ExtractedCourse(course_code=None, course_name=None, credits=3),  # dropped
            ExtractedCourse(course_name="Real Course", credits=999),  # credits rejected
        ],
        skills=["Python", "python", "  ", "SQL"],
    )
    _install_fake_client(monkeypatch, response=_FakeResponse(noisy))

    profile, _ = extract_academic_profile("text")

    assert profile.institution == "Riverbend State University"
    assert len(profile.major) <= llm_extraction.MAX_FIELD_CHARS
    assert len(profile.coursework) == 1
    assert profile.coursework[0].course_name == "Real Course"
    assert profile.coursework[0].credits is None
    assert profile.skills == ["Python", "SQL"]


def test_oversized_document_is_truncated_with_a_warning(monkeypatch):
    capture = {}
    _install_fake_client(monkeypatch, response=_FakeResponse(SAMPLE), capture=capture)

    _, warnings = extract_academic_profile("y" * (llm_extraction.MAX_LLM_INPUT_CHARS + 5000))

    assert any("only the first portion" in w for w in warnings)
    assert len(capture["messages"][0]["content"]) < llm_extraction.MAX_LLM_INPUT_CHARS + 1000


# --- failure modes: every one must raise LlmExtractionError ----------------


def test_refusal_raises(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(None, stop_reason="refusal"))
    with pytest.raises(LlmExtractionError):
        extract_academic_profile("text")


def test_truncated_output_raises(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(None, stop_reason="max_tokens"))
    with pytest.raises(LlmExtractionError):
        extract_academic_profile("text")


def test_missing_parsed_output_raises(monkeypatch):
    _install_fake_client(monkeypatch, response=_FakeResponse(None))
    with pytest.raises(LlmExtractionError):
        extract_academic_profile("text")


def test_api_status_error_raises_without_leaking_details(monkeypatch):
    api_error = anthropic.RateLimitError(
        "rate limited",
        response=httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")),
        body=None,
    )
    _install_fake_client(monkeypatch, error=api_error)

    with pytest.raises(LlmExtractionError) as excinfo:
        extract_academic_profile("SENSITIVE STUDENT TEXT")

    assert "SENSITIVE" not in str(excinfo.value)
    assert "429" in str(excinfo.value)


def test_connection_error_raises(monkeypatch):
    _install_fake_client(
        monkeypatch,
        error=anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")),
    )
    with pytest.raises(LlmExtractionError):
        extract_academic_profile("text")


def test_unexpected_error_is_contained(monkeypatch):
    _install_fake_client(monkeypatch, error=ValueError("boom"))
    with pytest.raises(LlmExtractionError):
        extract_academic_profile("text")
