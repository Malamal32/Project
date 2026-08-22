"""The two checks that compare produced text against its source.

`unsupported_numbers` was moved here from `service/resume_evidence.py` unchanged.
The first three tests pin the exact semantics the evidence contract depends on —
including the substring behaviour, which looks like a bug until you read why it
is deliberate — so a later "tidy-up" of this function fails here rather than
silently loosening the resume guardrail.
"""

from service.text_guards import (
    missing_protected_terms,
    protected_terms,
    unsupported_numbers,
)


# --- unsupported_numbers ---------------------------------------------------


def test_flags_the_invented_figure():
    """The canonical fabrication: a plausible, specific, entirely made-up metric
    attached to a real accomplishment."""
    assert unsupported_numbers(
        "optimized checkout flow, cutting load time 40%",
        "helped optimize the checkout flow",
    ) == ["40"]


def test_no_numbers_is_not_a_failure():
    assert unsupported_numbers("wrote documentation for the API", "wrote docs") == []


def test_matches_as_substring_of_the_evidence():
    """Deliberate, and load-bearing for the resume stage: "3" is supported by
    "3 credits" and "3.72" by "3.72/4.00". A whole-token match here would drop
    honest coursework claims."""
    assert unsupported_numbers("3 courses", "CS 310, 3 credits") == []
    assert unsupported_numbers("GPA 3.72", "cumulative 3.72/4.00") == []


def test_reports_every_unsupported_figure():
    found = unsupported_numbers("served 500 users across 12 sites", "served users at sites")
    assert found == ["500", "12"]


# --- protected_terms -------------------------------------------------------


def test_catches_names_mid_sentence():
    terms = protected_terms("Used Python and SQL to clean the data")
    assert "Python" in terms
    assert "SQL" in terms


def test_catches_punctuated_and_camelcase_names_anywhere():
    """Rule (a): interior capitals or interior + . # make a token a name in any
    position, including the start of a sentence."""
    terms = protected_terms("Node.js and PyTorch. C++ came later, plus C#")
    for name in ("Node.js", "PyTorch", "C++", "C#"):
        assert name in terms, f"{name} was not protected"


def test_a_lowercase_hyphenated_word_is_not_a_name():
    """The known under-protection, pinned so it is a decision rather than a
    surprise. "scikit-learn" is lost along with it — an interior hyphen cannot
    be told from the one in "auto-close", and protecting every hyphenated word
    would reject most honest rewrites."""
    assert protected_terms("wrote a script to auto-close stale on-campus tickets") == []
    assert protected_terms("used scikit-learn") == []


def test_ignores_a_sentence_initial_ordinary_word():
    """Rule (b) skips the first token of a sentence, so an ordinary capitalized
    verb is not mistaken for a product name. This is the check that stops good
    rewrites being rejected for rewording their own opening verb."""
    assert protected_terms("Answered support tickets") == []
    assert protected_terms("Built a dashboard. Wrote the tests.") == []


def test_deduplicates_case_insensitively():
    assert protected_terms("Used Python; later replaced Python entirely") == ["Python"]


def test_trailing_punctuation_is_not_part_of_the_name():
    assert protected_terms("Shipped with Django, then Flask.") == ["Django", "Flask"]


# --- missing_protected_terms -----------------------------------------------


def test_nothing_missing_when_the_rewrite_keeps_the_names():
    source = "Wrote ETL jobs in Python against Postgres"
    assert missing_protected_terms("Built ETL pipelines in Python against Postgres", source) == []


def test_reports_names_the_rewrite_dropped():
    """The failure the evidence contract cannot see: the shorter line is true,
    reads better, and has cost the student three verified skills.

    "ETL" is protected by rule (a) rather than rule (b) — an all-caps acronym is
    name-shaped wherever it appears, including at the start of a sentence.
    """
    source = "Wrote ETL jobs in Python against Postgres"
    assert missing_protected_terms("Built data pipelines for the analytics team", source) == [
        "ETL",
        "Python",
        "Postgres",
    ]


def test_uses_whole_word_matching():
    """Agrees with `market_matching._word_match`, which is what will read the
    polished text later. A substring test would call "R" present in "React" and
    pass a rewrite the matcher then fails on."""
    assert missing_protected_terms("Rewrote the Reactor module", "Shipped a React app") == ["React"]
