"""
Validation for untrusted, user-supplied PDF uploads.

Every check here treats the uploaded bytes as hostile input:
- extension and declared content-type are checked but never trusted alone
- the actual file signature (magic bytes) is checked
- size is capped before any parser ever touches the bytes
- password-protected / encrypted PDFs are rejected before MarkItDown runs
- malformed PDFs are caught and turned into a clean validation error,
  never an unhandled exception that could leak internals
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from service.config import ALLOWED_CONTENT_TYPES, ALLOWED_EXTENSION, MAX_UPLOAD_BYTES, PDF_MAGIC_BYTES


class PdfValidationError(Exception):
    """Raised for any reason an uploaded file must be rejected. Message is
    always safe to show the user — never includes raw file content."""


@dataclass
class ValidatedUpload:
    filename: str
    content: bytes


def validate_filename(filename: str) -> None:
    if not filename or not filename.lower().endswith(ALLOWED_EXTENSION):
        raise PdfValidationError("Only PDF files are accepted.")
    # Reject path separators / traversal attempts outright. The filename is
    # metadata only — it is NEVER used to build a filesystem path.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise PdfValidationError("Invalid filename.")


def validate_content_type(content_type: str | None) -> None:
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise PdfValidationError("Uploaded file must be application/pdf.")


def validate_size(num_bytes: int) -> None:
    if num_bytes == 0:
        raise PdfValidationError("Uploaded file is empty.")
    if num_bytes > MAX_UPLOAD_BYTES:
        raise PdfValidationError(f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.")


def validate_signature(content: bytes) -> None:
    if not content.startswith(PDF_MAGIC_BYTES):
        raise PdfValidationError("File does not look like a valid PDF.")


def validate_not_encrypted_and_parseable(content: bytes) -> PdfReader:
    """Also doubles as the malformed-PDF check: PdfReader raising here means
    the file is not a well-formed PDF, independent of the magic-byte check."""
    try:
        reader = PdfReader(io.BytesIO(content))
    except PdfReadError as exc:
        raise PdfValidationError("PDF could not be read — it may be corrupted.") from exc
    except Exception as exc:  # defensive: pypdf can raise several exception types
        raise PdfValidationError("PDF could not be read — it may be corrupted.") from exc

    if reader.is_encrypted:
        raise PdfValidationError("This PDF is password-protected. Please upload an unprotected copy.")
    if len(reader.pages) == 0:
        raise PdfValidationError("PDF has no readable pages.")
    return reader


def validate_upload(filename: str, content_type: str | None, content: bytes) -> ValidatedUpload:
    """Runs the full validation pipeline. Raises PdfValidationError on any
    failure; callers should turn that into a 4xx response."""
    validate_filename(filename)
    validate_content_type(content_type)
    validate_size(len(content))
    validate_signature(content)
    validate_not_encrypted_and_parseable(content)
    return ValidatedUpload(filename=os.path.basename(filename), content=content)
