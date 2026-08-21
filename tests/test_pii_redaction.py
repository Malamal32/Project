import pytest

from pipeline.pii_redaction import redact_pii, validate_redacted


def test_email_is_redacted():
    text = "Reach out at jane.doe@example.com for details."
    redacted, matches = redact_pii(text)
    assert "jane.doe@example.com" not in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert matches[0].kind == "email"


def test_phone_is_redacted():
    text = "Call us at (415) 555-0100 today."
    redacted, matches = redact_pii(text)
    assert "555-0100" not in redacted
    assert "[REDACTED_PHONE]" in redacted
    assert matches[0].kind == "phone"


def test_recruiter_name_after_label_is_redacted():
    text = "Recruiter: Jane Smith will follow up."
    redacted, matches = redact_pii(text)
    assert "Jane Smith" not in redacted
    assert "[REDACTED_NAME]" in redacted
    assert matches[0].kind == "recruiter_name"


def test_multiple_pii_kinds_in_one_text_are_all_redacted():
    text = "Contact: Jane Smith at jane@acme.com or (415) 555-0100."
    redacted, matches = redact_pii(text)
    assert {m.kind for m in matches} == {"recruiter_name", "email", "phone"}
    assert "Jane Smith" not in redacted
    assert "jane@acme.com" not in redacted
    assert "555-0100" not in redacted


def test_plain_text_is_unchanged():
    text = "We build great products for great people."
    redacted, matches = redact_pii(text)
    assert redacted == text
    assert matches == []


def test_validate_redacted_raises_on_leftover_email():
    with pytest.raises(ValueError):
        validate_redacted("still has bob@example.com in it")


def test_validate_redacted_raises_on_leftover_phone():
    with pytest.raises(ValueError):
        validate_redacted("call 415-555-0100 now")


def test_validate_redacted_passes_on_clean_text():
    validate_redacted("nothing sensitive here")
