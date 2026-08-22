"""The LinkedIn export mapper.

Two things are actually under test here, and only one of them is CSV parsing.
The other is the whitelist: an import that reads a file it was never supposed to
open puts other people's names, employers and private messages into a request.
That is the case worth a test that fails loudly.
"""

from __future__ import annotations

import pytest

from service.config import MAX_LINKEDIN_FIELD_CHARS, MAX_LINKEDIN_ROWS
from service.linkedin_import import EXPORT_FILES, is_wanted, member_basename, parse_export

POSITIONS = (
    "Company Name,Title,Description,Location,Started On,Finished On\n"
    "Riverbend Analytics,Data Analyst Intern,"
    '"Built dashboards for the ops team.\nCut a weekly report from 3 hours to 20 minutes.",'
    "Austin TX,Jun 2025,Aug 2025\n"
    "Campus IT Help Desk,Student Technician,Answered tickets.,Riverbend,Sep 2024,\n"
)

PROJECTS = (
    "Title,Description,Url,Started On,Finished On\n"
    "Transit Delay Tracker,Scraped and charted bus arrival times.,"
    "https://github.com/jordan/transit,Jan 2025,Mar 2025\n"
)

SKILLS = "Name\nPython\nSQL\npython\n"
CERTIFICATIONS = "Name,Url,Authority,Started On,Finished On,License Number\nAWS Cloud Practitioner,,AWS,Feb 2025,,ABC123\n"
HONORS = "Title,Description,Issued On\nDean's List,Top 10% of the class,Dec 2024\n"


def test_positions_become_experience_verbatim():
    result = parse_export({"Positions.csv": POSITIONS})

    assert [e.title for e in result.experience] == ["Data Analyst Intern", "Student Technician"]
    first = result.experience[0]
    assert first.organization == "Riverbend Analytics"
    assert first.location == "Austin TX"
    # Dates are not parsed into anything. "Jun 2025" is what the archive said,
    # and the same rule that governs `models/student.py` governs here.
    assert (first.started_on, first.finished_on) == ("Jun 2025", "Aug 2025")
    assert "Cut a weekly report" in first.description

    # A blank Finished On stays blank on the wire — the browser is the one that
    # reads it as "Present", where the form's own label says so.
    assert result.experience[1].finished_on is None


def test_reads_projects_skills_certifications_and_honors():
    result = parse_export(
        {
            "Projects.csv": PROJECTS,
            "Skills.csv": SKILLS,
            "Certifications.csv": CERTIFICATIONS,
            "Honors.csv": HONORS,
        }
    )

    assert result.projects[0].name == "Transit Delay Tracker"
    assert result.projects[0].url == "https://github.com/jordan/transit"
    # No technologies column exists in the archive, and one is not inferred.
    assert result.projects[0].description.startswith("Scraped")

    # Deduped case-insensitively, order preserved.
    assert result.skills == ["Python", "SQL"]
    # The authority and licence number sit right beside the name and are not
    # imported — a resume lists the certification, not its paperwork.
    assert result.certifications == ["AWS Cloud Practitioner"]
    assert result.honors == ["Dean's List"]


def test_output_is_independent_of_the_order_files_arrive_in():
    forwards = parse_export({"Positions.csv": POSITIONS, "Skills.csv": SKILLS})
    backwards = parse_export({"Skills.csv": SKILLS, "Positions.csv": POSITIONS})
    assert forwards.model_dump() == backwards.model_dump()


# --- the whitelist ---------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Connections.csv",
        "messages.csv",
        "Ad_Targeting.csv",
        "PhoneNumbers.csv",
        "Inferences_about_you.csv",
        "Rich_Media.csv",
    ],
)
def test_files_outside_the_whitelist_are_never_parsed(name):
    """The archive members that carry other people's data. Reaching this module
    at all means the browser's own filter let them through, so this is the
    second of two independent checks, not the only one."""
    result = parse_export({name: "Name,Email\nSomeone Else,someone@example.com\n"})

    assert result.experience == []
    assert result.skills == []
    assert [f.name for f in result.files_ignored] == [name]
    assert result.files_read == []


def test_a_whitelisted_file_inside_a_folder_is_still_matched():
    result = parse_export({"Basic_LinkedInDataExport_2026/Positions.csv": POSITIONS})
    assert len(result.experience) == 2
    assert is_wanted("Basic_LinkedInDataExport_2026\\Positions.csv")
    assert member_basename("Export/Positions.csv") == "positions.csv"


def test_both_spellings_of_honors_are_recognised():
    assert parse_export({"Honours.csv": HONORS}).honors == ["Dean's List"]
    assert parse_export({"Honors.csv": HONORS}).honors == ["Dean's List"]


# --- files that do not read cleanly ----------------------------------------


def test_an_unrecognised_header_is_reported_not_guessed_at():
    result = parse_export({"Positions.csv": "Foo,Bar\n1,2\n"})

    assert result.experience == []
    assert result.files_read == []
    assert len(result.files_ignored) == 1
    assert "columns" in result.files_ignored[0].reason


def test_a_notes_preamble_above_the_header_is_skipped():
    """Some archive vintages put a notes block above the column names."""
    result = parse_export({"Positions.csv": 'Notes:,\n"This file lists your positions.",\n\n' + POSITIONS})
    assert [e.title for e in result.experience] == ["Data Analyst Intern", "Student Technician"]


def test_an_older_column_spelling_still_reads():
    result = parse_export(
        {"Positions.csv": "Company,Position,Description,Start Date,End Date\nAcme,Intern,Did things.,Jun 2025,Aug 2025\n"}
    )
    assert result.experience[0].organization == "Acme"
    assert result.experience[0].title == "Intern"
    assert result.experience[0].started_on == "Jun 2025"


def test_one_unreadable_file_does_not_cost_the_others():
    result = parse_export({"Positions.csv": POSITIONS, "Projects.csv": "Foo\n1\n"})

    assert len(result.experience) == 2
    assert result.projects == []
    assert [f.name for f in result.files_ignored] == ["Projects.csv"]


def test_blank_rows_are_dropped():
    result = parse_export({"Positions.csv": POSITIONS + ",,,,,\n,,,,,\n"})
    assert len(result.experience) == 2


def test_row_count_is_bounded_and_the_student_is_told():
    header = "Company Name,Title,Description,Started On,Finished On\n"
    body = "".join(f"Acme,Role {i},Did things.,Jun 2025,Aug 2025\n" for i in range(MAX_LINKEDIN_ROWS + 20))
    result = parse_export({"Positions.csv": header + body})

    assert len(result.experience) == MAX_LINKEDIN_ROWS
    assert any(str(MAX_LINKEDIN_ROWS) in w for w in result.warnings)


def test_a_long_description_is_truncated_out_loud():
    header = "Company Name,Title,Description\n"
    result = parse_export({"Positions.csv": header + f"Acme,Analyst,{'x' * (MAX_LINKEDIN_FIELD_CHARS + 500)}\n"})

    assert len(result.experience[0].description) == MAX_LINKEDIN_FIELD_CHARS
    assert any("shortened" in w for w in result.warnings)


def test_an_archive_with_nothing_readable_says_so():
    result = parse_export({"Connections.csv": "Name\nSomeone\n"})
    assert any("didn't find any of the files" in w for w in result.warnings)


def test_an_export_with_no_positions_says_so_rather_than_looking_empty():
    result = parse_export({"Skills.csv": SKILLS})
    assert result.files_read == ["Skills.csv"]
    assert any("No work experience" in w for w in result.warnings)


def test_nothing_in_the_whitelist_is_a_connections_or_messages_file():
    """A guard on the list itself, not on the code that uses it. The failure
    this catches is someone adding a file to EXPORT_FILES without thinking about
    whose data is in it."""
    forbidden = {"connections.csv", "messages.csv", "invitations.csv", "contacts.csv"}
    assert not forbidden & set(EXPORT_FILES)
