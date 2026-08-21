"""The rule-based fallback extractor. Pure function, no network, no database —
it must never fabricate a field that isn't textually present."""

from service.academic_extraction import normalize_academic_text

SAMPLE_MARKDOWN = """
Riverbend State University
Official Academic Transcript

Bachelor of Science
Major: Computer Science
Minor: Mathematics
Expected Graduation: May 2027
GPA: 3.72/4.00

## Coursework
CS 310 - Data Structures and Algorithms - 3 cr - Grade: A - Fall 2025
CS 340 - Database Systems - 3 cr - Grade: A- - Spring 2025
MATH 251 - Statistics I - 3 cr - Grade: B+ - Fall 2024

## Honors
- Dean's List, Fall 2025

## Skills
- Python
- SQL
- Git

## Certifications
- AWS Cloud Practitioner
"""


def test_extracts_expected_fields():
    profile, warnings = normalize_academic_text(SAMPLE_MARKDOWN)
    assert profile.institution == "Riverbend State University"
    assert profile.degree == "Bachelor of Science"
    assert profile.degree_level == "Bachelor's"
    assert profile.major == "Computer Science"
    assert profile.minor == "Mathematics"
    assert profile.expected_graduation_date.startswith("May 2027")
    assert profile.gpa == "3.72/4.00"
    assert len(profile.coursework) == 3
    assert profile.coursework[0].course_code == "CS 310"
    assert profile.coursework[0].student_approved is False
    assert "Dean's List, Fall 2025" in profile.honors
    assert "AWS Cloud Practitioner" in profile.certifications
    assert profile.skills == ["Python", "SQL", "Git"]
    assert warnings == []


def test_never_fabricates_missing_fields():
    profile, warnings = normalize_academic_text("Just some unrelated text with no structure.")
    assert profile.institution is None
    assert profile.degree is None
    assert profile.major is None
    assert profile.gpa is None
    assert profile.coursework == []
    assert len(warnings) > 0


def test_gpa_not_extracted_when_absent():
    profile, _ = normalize_academic_text("Bachelor of Science\nMajor: Biology\nNo grade info here.")
    assert profile.gpa is None


def test_values_are_kept_verbatim():
    """The same rule the LLM prompt states: copy what the document says. A date
    must not be reformatted, and a GPA must keep its scale."""
    profile, _ = normalize_academic_text(SAMPLE_MARKDOWN)
    assert profile.expected_graduation_date == "May 2027"
    assert profile.gpa == "3.72/4.00"
