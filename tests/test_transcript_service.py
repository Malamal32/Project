"""End-to-end transcript parsing: bytes in, ParseResponse out.

The autouse `_no_live_api_calls` fixture strips Anthropic credentials, so by
default these exercise the rule-based fallback path. The two LLM tests patch
`llm_extraction` rather than reaching the network.
"""

import glob
import tempfile
from pathlib import Path
from unittest.mock import patch

from service import llm_extraction
from service.schemas import AcademicProfile
from service.transcript_service import parse_transcript_bytes


async def test_valid_transcript_end_to_end(synthetic_transcript_pdf):
    response = await parse_transcript_bytes("transcript.pdf", "application/pdf", synthetic_transcript_pdf)
    assert response.success is True
    assert response.review_required is True
    assert response.extraction_method == "rules"
    assert response.academic_profile.institution == "Riverbend State University"
    assert response.academic_profile.gpa == "3.72/4.00"
    assert len(response.academic_profile.coursework) == 3


async def test_minimal_document_returns_warnings_not_fabrication(synthetic_minimal_pdf):
    response = await parse_transcript_bytes("doc.pdf", "application/pdf", synthetic_minimal_pdf)
    assert response.success is True
    assert response.academic_profile.institution is None
    assert len(response.warnings) > 0


async def test_scanned_image_only_pdf_falls_back(synthetic_blank_pdf):
    response = await parse_transcript_bytes("scan.pdf", "application/pdf", synthetic_blank_pdf)
    assert response.success is False
    assert response.review_required is True
    assert any("scanned" in w.lower() for w in response.warnings)


async def test_encrypted_pdf_is_rejected(synthetic_encrypted_pdf):
    response = await parse_transcript_bytes("locked.pdf", "application/pdf", synthetic_encrypted_pdf)
    assert response.success is False
    assert any("password" in w.lower() for w in response.warnings)


async def test_wrong_extension_is_rejected(synthetic_transcript_pdf):
    response = await parse_transcript_bytes("transcript.txt", "application/pdf", synthetic_transcript_pdf)
    assert response.success is False
    assert any("PDF" in w for w in response.warnings)


async def test_traversal_in_filename_is_rejected(synthetic_transcript_pdf):
    response = await parse_transcript_bytes("../../etc/passwd.pdf", "application/pdf", synthetic_transcript_pdf)
    assert response.success is False


async def test_wrong_content_type_is_rejected(synthetic_transcript_pdf):
    response = await parse_transcript_bytes("transcript.pdf", "text/plain", synthetic_transcript_pdf)
    assert response.success is False


async def test_bad_signature_is_rejected():
    response = await parse_transcript_bytes("fake.pdf", "application/pdf", b"not a real pdf")
    assert response.success is False


async def test_oversized_file_is_rejected(synthetic_transcript_pdf):
    from service.config import MAX_UPLOAD_BYTES

    oversized = synthetic_transcript_pdf + b"0" * (MAX_UPLOAD_BYTES + 1)
    response = await parse_transcript_bytes("big.pdf", "application/pdf", oversized)
    assert response.success is False
    assert any("MB" in w for w in response.warnings)


async def test_empty_file_is_rejected():
    response = await parse_transcript_bytes("empty.pdf", "application/pdf", b"")
    assert response.success is False


async def test_llm_extraction_is_used_when_enabled(synthetic_transcript_pdf):
    """When the Claude API stage succeeds, its profile is what ships."""
    profile = AcademicProfile(institution="From The Model University", degree="Bachelor of Arts")
    with patch.object(llm_extraction, "is_enabled", return_value=True), patch.object(
        llm_extraction, "extract_academic_profile", return_value=(profile, [])
    ):
        response = await parse_transcript_bytes("t.pdf", "application/pdf", synthetic_transcript_pdf)

    assert response.success is True
    assert response.extraction_method == "llm"
    assert response.academic_profile.institution == "From The Model University"


async def test_llm_failure_falls_back_to_rules_without_failing_the_upload(synthetic_transcript_pdf):
    """An API outage degrades result quality — it must not break the request."""
    with patch.object(llm_extraction, "is_enabled", return_value=True), patch.object(
        llm_extraction,
        "extract_academic_profile",
        side_effect=llm_extraction.LlmExtractionError("API error 503"),
    ):
        response = await parse_transcript_bytes("t.pdf", "application/pdf", synthetic_transcript_pdf)

    assert response.success is True
    assert response.extraction_method == "rules"
    assert response.academic_profile.institution == "Riverbend State University"


async def test_markitdown_failure_does_not_leave_temp_file(synthetic_transcript_pdf):
    """Confirms cleanup runs even when MarkItDown raises."""
    with patch("service.transcript_service._markitdown.convert_local", side_effect=RuntimeError("boom")):
        response = await parse_transcript_bytes("transcript.pdf", "application/pdf", synthetic_transcript_pdf)

    assert response.success is False
    leftover = glob.glob(str(Path(tempfile.gettempdir()) / "transcript_*"))
    assert leftover == []


async def test_parsing_writes_nothing_to_the_database(synthetic_transcript_pdf, db_session):
    """The privacy boundary: parsing a transcript must not touch a table."""
    from sqlalchemy import func, select

    from models.student import StudentProfile

    await parse_transcript_bytes("transcript.pdf", "application/pdf", synthetic_transcript_pdf)

    assert db_session.execute(select(func.count()).select_from(StudentProfile)).scalar_one() == 0
