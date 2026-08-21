"""
Normalizes MarkItDown's extracted Markdown/text into a structured
AcademicProfile. Purely rule-based (regex/heuristics) — never fabricates a
field that isn't textually present in the document. Anything not confidently
found is left as None/empty, and the frontend requires the student to review
and approve every field before it can be saved.

This is the *fallback* path. The primary extractor is `service.llm_extraction`;
this one runs when the API is disabled, unreachable, or returns something
unusable, so an outage degrades result quality instead of failing the upload.
"""

from __future__ import annotations

import re
import uuid
from typing import List, Optional, Tuple

from service.schemas import AcademicProfile, Coursework

DEGREE_PATTERNS = [
    (r"\bBachelor of Science\b", "Bachelor of Science", "Bachelor's"),
    (r"\bBachelor of Arts\b", "Bachelor of Arts", "Bachelor's"),
    (r"\bB\.?S\.?\b", "Bachelor of Science", "Bachelor's"),
    (r"\bB\.?A\.?\b", "Bachelor of Arts", "Bachelor's"),
    (r"\bMaster of Science\b", "Master of Science", "Master's"),
    (r"\bMaster of Arts\b", "Master of Arts", "Master's"),
    (r"\bM\.?S\.?\b", "Master of Science", "Master's"),
    (r"\bAssociate of (Arts|Science)\b", "Associate Degree", "Associate"),
    (r"\bDoctor of Philosophy\b|\bPh\.?D\.?\b", "Doctor of Philosophy", "Doctorate"),
]

INSTITUTION_KEYWORDS = re.compile(
    r"([A-Z][A-Za-z&.,'\- ]{2,80}?\b(?:University|College|Institute of Technology|Institute))\b"
)
MAJOR_LABEL = re.compile(r"(?:Major|Program of Study)\s*[:\-]\s*(.+)", re.IGNORECASE)
MINOR_LABEL = re.compile(r"Minor\s*[:\-]\s*(.+)", re.IGNORECASE)
CONCENTRATION_LABEL = re.compile(r"Concentration\s*[:\-]\s*(.+)", re.IGNORECASE)
EXPECTED_GRAD_LABEL = re.compile(
    r"Expected\s+Graduation(?:\s+Date)?\s*[:\-]\s*([A-Za-z0-9 ,/]+)", re.IGNORECASE
)
GRAD_DATE_LABEL = re.compile(
    r"(?:Graduation Date|Date Conferred|Conferred)\s*[:\-]\s*([A-Za-z0-9 ,/]+)", re.IGNORECASE
)
GPA_LABEL = re.compile(r"GPA\s*[:\-]?\s*(\d\.\d{1,2})\s*(?:/\s*(\d\.\d{1,2}))?", re.IGNORECASE)

COURSE_LINE = re.compile(
    r"^\s*(?P<code>[A-Z]{2,5}\s?-?\d{3,4})\s*[-:]?\s*(?P<name>[A-Za-z][A-Za-z0-9 &,/'\-]{2,80})"
    r"(?:.*?(?P<credits>\d(?:\.\d)?)\s*(?:cr|credits?))?"
    r"(?:.*?\bGrade\s*[:\-]?\s*(?P<grade>[A-F][+-]?))?"
    r"(?:.*?\b(?P<term>(?:Fall|Spring|Summer|Winter)\s?\d{4}))?",
    re.IGNORECASE,
)

SECTION_HEADING = re.compile(r"^#{1,6}\s*(.+)$")


def _find_section(lines: List[str], heading_pattern: re.Pattern) -> List[str]:
    """Returns the non-empty lines under the first heading matching
    heading_pattern, up to the next heading of equal-or-higher level."""
    collected: List[str] = []
    capturing = False
    for line in lines:
        heading_match = SECTION_HEADING.match(line.strip())
        if heading_match:
            if capturing:
                break
            if heading_pattern.search(heading_match.group(1)):
                capturing = True
            continue
        if capturing and line.strip():
            collected.append(line.strip())
    return collected


def _extract_degree(text: str) -> Tuple[Optional[str], Optional[str]]:
    for pattern, degree_name, level in DEGREE_PATTERNS:
        if re.search(pattern, text):
            return degree_name, level
    return None, None


def _extract_institution(text: str) -> Optional[str]:
    match = INSTITUTION_KEYWORDS.search(text)
    return match.group(1).strip() if match else None


def _extract_labeled(pattern: re.Pattern, text: str) -> Optional[str]:
    match = pattern.search(text)
    return match.group(1).strip().rstrip(".") if match else None


def _extract_gpa(text: str) -> Optional[str]:
    match = GPA_LABEL.search(text)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}" if match.group(2) else match.group(1)


def _extract_coursework(lines: List[str]) -> List[Coursework]:
    section = _find_section(lines, re.compile(r"coursework|courses taken|relevant courses", re.IGNORECASE))
    courses: List[Coursework] = []
    for line in section:
        match = COURSE_LINE.match(line)
        if not match:
            continue
        credits_raw = match.group("credits")
        courses.append(
            Coursework(
                id=str(uuid.uuid4()),
                course_code=match.group("code").strip() if match.group("code") else None,
                course_name=match.group("name").strip() if match.group("name") else None,
                credits=float(credits_raw) if credits_raw else None,
                grade=match.group("grade"),
                term=match.group("term"),
                student_approved=False,
            )
        )
    return courses


def _extract_list_section(lines: List[str], heading_pattern: re.Pattern) -> List[str]:
    section = _find_section(lines, heading_pattern)
    items = []
    for line in section:
        cleaned = re.sub(r"^[-*•\d.\)]+\s*", "", line).strip()
        if cleaned:
            items.append(cleaned)
    return items


def missing_field_warnings(profile: AcademicProfile) -> List[str]:
    """Student-facing notes about fields that still need attention. Shared with
    the Claude API extraction path so both stages say the same thing."""
    warnings: List[str] = []
    if not profile.institution:
        warnings.append("Could not confidently identify an institution name. Please fill it in.")
    if not profile.degree:
        warnings.append("Could not confidently identify a degree. Please fill it in.")
    if not profile.major:
        warnings.append("Could not confidently identify a major. Please fill it in.")
    if not profile.coursework:
        warnings.append("No coursework list was detected. You can add courses manually.")
    return warnings


def normalize_academic_text(markdown_text: str) -> Tuple[AcademicProfile, List[str]]:
    """Pure function: Markdown/text in, structured profile + warnings out.
    Never invents a value that isn't present in markdown_text."""
    lines = markdown_text.splitlines()

    degree, degree_level = _extract_degree(markdown_text)
    institution = _extract_institution(markdown_text)
    major = _extract_labeled(MAJOR_LABEL, markdown_text)
    minor = _extract_labeled(MINOR_LABEL, markdown_text)
    concentration = _extract_labeled(CONCENTRATION_LABEL, markdown_text)
    expected_grad = _extract_labeled(EXPECTED_GRAD_LABEL, markdown_text)
    grad_date = _extract_labeled(GRAD_DATE_LABEL, markdown_text)
    gpa = _extract_gpa(markdown_text)
    coursework = _extract_coursework(lines)
    skills = _extract_list_section(lines, re.compile(r"technical skills|skills(?:\s*&\s*technologies)?", re.IGNORECASE))
    honors = _extract_list_section(lines, re.compile(r"honors|awards|dean'?s list", re.IGNORECASE))
    certifications = _extract_list_section(lines, re.compile(r"certifications?|licenses?", re.IGNORECASE))

    profile = AcademicProfile(
        institution=institution,
        degree=degree,
        degree_level=degree_level,
        major=major,
        minor=minor,
        concentration=concentration,
        graduation_date=grad_date,
        expected_graduation_date=expected_grad,
        gpa=gpa,
        coursework=coursework,
        skills=skills,
        honors=honors,
        certifications=certifications,
    )
    return profile, missing_field_warnings(profile)
