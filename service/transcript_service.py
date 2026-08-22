"""
Transcript -> text -> Claude API extraction -> structured JSON.

There are two entry points, differing only in where the text comes from:

- `parse_transcript_bytes` takes an uploaded PDF and converts it server-side
  with MarkItDown. Server-only: MarkItDown has native dependencies and cannot
  run on Cloudflare Python Workers.
- `parse_transcript_text` takes text the *browser* already extracted with
  pdf.js (`frontend/js/services/academic-extraction.js`). This is the path the
  deployed Worker uses, and it is strictly less exposed: the PDF bytes never
  leave the student's machine at all.

Both converge on `_extract`, so the extraction rules, the fallback behavior,
and the `extraction_method` reporting are identical either way.

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
from starlette.concurrency import run_in_threadpool

from service import llm_extraction
from service.academic_extraction import normalize_academic_text
from service.config import MIN_EXTRACTED_CHARS
from service.pdf_validation import PdfValidationError, validate_upload
from service.schemas import AcademicProfile, ParseResponse

log = structlog.get_logger()

_markitdown = None


def _get_markitdown():
    """Imported on first use rather than at module import.

    MarkItDown has native dependencies and is absent on Cloudflare Python
    Workers. Only `parse_transcript_bytes` needs it, and the Worker never calls
    that path — so importing it lazily is what lets this module be shared
    between the two runtimes instead of forked.

    Plugins disabled: we only need the built-in PDF text-extraction path, and
    plugins are the vector by which MarkItDown could reach out to fetch remote
    content. We also never call convert_url()/convert() on anything but a
    server-controlled local temp file path, so there is no way for a student's
    upload to make this service fetch an arbitrary URL.
    """
    global _markitdown
    if _markitdown is None:
        from markitdown import MarkItDown

        _markitdown = MarkItDown(enable_plugins=False)
    return _markitdown


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
        result = _get_markitdown().convert_local(tmp_path)
        return result.text_content or ""
    finally:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass


async def parse_transcript_bytes(filename: str, content_type: str | None, content: bytes) -> ParseResponse:
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
        # pypdf and MarkItDown are synchronous and CPU-bound; running them
        # inline would stall the event loop for every other in-flight request.
        # (This path never executes on Workers, which have no threads — the
        # Worker uses parse_transcript_text instead.)
        extracted_text = await run_in_threadpool(
            _run_markitdown_on_temp_file, validated.content
        )
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

    return await _profile_from_text(extracted_text)


async def parse_transcript_text(text: str) -> ParseResponse:
    """Entry point for text the browser extracted from the PDF itself.

    Applies the same minimum-length check as the server-side path: the browser
    reports pdf.js's text layer verbatim, and a scanned document yields an
    empty one there for exactly the same reason it does here.
    """
    log.info("transcript.received_text", chars=len(text))

    if len(text.strip()) < MIN_EXTRACTED_CHARS:
        log.info("transcript.scanned_or_empty", extracted_chars=len(text.strip()))
        return ParseResponse(
            success=False,
            academic_profile=AcademicProfile(),
            warnings=[
                "This document appears to be a scanned image with no selectable "
                "text, so we couldn't extract information from it. Please enter "
                "your academic information manually."
            ],
            review_required=True,
        )

    return await _profile_from_text(text)


async def _profile_from_text(extracted_text: str) -> ParseResponse:
    profile, warnings, method = await _extract(extracted_text)
    return ParseResponse(
        success=True,
        academic_profile=profile,
        warnings=warnings,
        review_required=True,
        extraction_method=method,
    )


async def _extract(extracted_text: str) -> Tuple[AcademicProfile, List[str], str]:
    """Claude API extraction with automatic fallback to the rule-based
    normalizer. An API outage, a bad key, or a rate limit degrades the quality
    of the result — it never fails the upload."""
    if not llm_extraction.is_enabled():
        profile, warnings = normalize_academic_text(extracted_text)
        return profile, warnings, "rules"

    try:
        profile, warnings = await llm_extraction.extract_academic_profile(extracted_text)
        return profile, warnings, "llm"
    except llm_extraction.LlmExtractionError as exc:
        # Reason only — never the document text or the model output.
        log.warning("transcript.llm_fallback_to_rules", reason=str(exc))
        profile, warnings = normalize_academic_text(extracted_text)
        return profile, warnings, "rules"
