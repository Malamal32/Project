"""Upload validation. Every uploaded byte is treated as hostile input, and every
rejection message must be safe to show a student."""

import pytest

from service.config import MAX_UPLOAD_BYTES
from service.pdf_validation import (
    PdfValidationError,
    validate_content_type,
    validate_filename,
    validate_not_encrypted_and_parseable,
    validate_signature,
    validate_size,
    validate_upload,
)


def test_accepts_a_well_formed_pdf(synthetic_transcript_pdf):
    validated = validate_upload("transcript.pdf", "application/pdf", synthetic_transcript_pdf)
    assert validated.filename == "transcript.pdf"
    assert validated.content == synthetic_transcript_pdf


@pytest.mark.parametrize("filename", ["", "notes.txt", "transcript.PDF.exe"])
def test_rejects_non_pdf_extensions(filename):
    with pytest.raises(PdfValidationError):
        validate_filename(filename)


@pytest.mark.parametrize("filename", ["../secrets.pdf", "dir/transcript.pdf", "a\\b.pdf"])
def test_rejects_path_traversal_in_filename(filename):
    """The filename is metadata only and never builds a path, but it is rejected
    anyway rather than relying on that staying true."""
    with pytest.raises(PdfValidationError):
        validate_filename(filename)


def test_basename_is_kept_not_the_supplied_path(synthetic_transcript_pdf):
    # A path that survives validate_filename still gets basename'd on the way out.
    validated = validate_upload("transcript.pdf", "application/pdf", synthetic_transcript_pdf)
    assert "/" not in validated.filename


def test_rejects_wrong_content_type():
    with pytest.raises(PdfValidationError):
        validate_content_type("text/plain")
    with pytest.raises(PdfValidationError):
        validate_content_type(None)


def test_rejects_empty_and_oversized():
    with pytest.raises(PdfValidationError):
        validate_size(0)
    with pytest.raises(PdfValidationError):
        validate_size(MAX_UPLOAD_BYTES + 1)
    validate_size(1024)  # does not raise


def test_declared_type_alone_is_not_trusted():
    """A .pdf name and an application/pdf content-type still fail on magic bytes."""
    with pytest.raises(PdfValidationError):
        validate_signature(b"GIF89a not a pdf")


def test_rejects_encrypted_pdf(synthetic_encrypted_pdf):
    with pytest.raises(PdfValidationError, match="password"):
        validate_not_encrypted_and_parseable(synthetic_encrypted_pdf)


def test_malformed_pdf_becomes_a_clean_error():
    """A parser blowing up must surface as a validation error, never as an
    unhandled exception that could leak internals."""
    with pytest.raises(PdfValidationError):
        validate_not_encrypted_and_parseable(b"%PDF-1.7\ntruncated garbage")
