"""
PDF -> validation -> temporary processing -> MarkItDown -> Claude API
extraction -> structured JSON -> delete temporary file.

Every uploaded PDF is treated as untrusted. No uploaded document or its
extracted text is ever persisted to disk beyond the lifetime of a single
request, logged, or written to `raw_documents`/R2 — the raw-payload store in
`pipeline/raw_store.py` is for job postings, and a transcript never goes near
it. The extracted text IS sent to the Anthropic API for the extraction step —
see README -> Transcript extraction service -> Data handling.

Only the structured profile, after the student reviews it, is written to the
database, and only through `service.profile_store` on a separate request.
"""

from __future__ import annotations

import os
import tempfile
from typing import List, Tuple

import structlog
from markitdown import MarkItDown

from service import llm_extraction
from service.academic_extraction import normalize_academic_text
from service.config import MIN_EXTRACTED_CHARS
from service.pdf_validation import PdfValidationError, validate_upload
from service.schemas import AcademicProfile, ParseResponse

log = structlog.get_logger()

# Plugins disabled: we only need the built-in PDF text-extraction path, and
# plugins are the vector by which MarkItDown could reach out to fetch remote
# content. We also never call convert_url()/convert() on anything but a
# server-controlled local temp file path, so there is no way for a student's
# upload to make this service fetch an arbitrary URL.
_markitdown = MarkItDown(enable_plugins=False)


class ScannedDocumentError(Exception):
    """Raised when MarkItDown returns effectively no extractable text —
    almost always a scanned/image-only PDF with no text layer."""


def _run_markitdown_on_temp_file(content: bytes) -> str:
    """Writes to a server-controlled temp path (random name, our temp dir),
    converts with MarkItDown's narrowest local-file operation, and guarantees
    cleanup via try/finally regardless of success or failure."""
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="transcript_")
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(content)
        result = _markitdown.convert_local(tmp_path)
        return result.text_content or ""
    finally:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass


def parse_transcript_bytes(filename: str, content_type: str | None, content: bytes) -> ParseResponse:
    """Full pipeline for one upload. Never logs `content` or extracted text —
    only non-sensitive metadata (byte size, success/failure) is logged."""
    log.info("transcript.received", size_bytes=len(content))

    try:
        validated = validate_upload(filename, content_type, content)
    except PdfValidationError as exc:
        log.info("transcript.rejected", reason=str(exc))
        return ParseResponse(
            success=False,
            academic_profile=AcademicProfile(),
            warnings=[str(exc)],
            review_required=True,
        )

    try:
        extracted_text = _run_markitdown_on_temp_file(validated.content)
    except Exception:
        log.exception("transcript.conversion_failed")
        return ParseResponse(
            success=False,
            academic_profile=AcademicProfile(),
            warnings=[
                "We couldn't process this PDF. Please try again or enter your "
                "academic information manually."
            ],
            review_required=True,
        )

    if len(extracted_text.strip()) < MIN_EXTRACTED_CHARS:
        log.info("transcript.scanned_or_empty", extracted_chars=len(extracted_text.strip()))
        return ParseResponse(
            success=False,
            academic_profile=AcademicProfile(),
            warnings=[
                "This PDF appears to be a scanned image with no selectable text, "
                "so we couldn't extract information from it. Please enter your "
                "academic information manually."
            ],
            review_required=True,
        )

    profile, warnings, method = _extract(extracted_text)
    return ParseResponse(
        success=True,
        academic_profile=profile,
        warnings=warnings,
        review_required=True,
        extraction_method=method,
    )


def _extract(extracted_text: str) -> Tuple[AcademicProfile, List[str], str]:
    """Claude API extraction with automatic fallback to the rule-based
    normalizer. An API outage, a bad key, or a rate limit degrades the quality
    of the result — it never fails the upload."""
    if not llm_extraction.is_enabled():
        profile, warnings = normalize_academic_text(extracted_text)
        return profile, warnings, "rules"

    try:
        profile, warnings = llm_extraction.extract_academic_profile(extracted_text)
        return profile, warnings, "llm"
    except llm_extraction.LlmExtractionError as exc:
        # Reason only — never the document text or the model output.
        log.warning("transcript.llm_fallback_to_rules", reason=str(exc))
        profile, warnings = normalize_academic_text(extracted_text)
        return profile, warnings, "rules"
