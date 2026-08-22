"""Checks that compare model-written text against the text it was drawn from.

Two stages need these and neither should import the other. `resume_evidence`
validates generated claims against the profile items they cite;
`description_polish` validates a rewritten description against the student's
original notes. The checks are the same shape — "what does the produced text
assert that the source does not support?" — so they live here, in a leaf both
can import.

The two guards answer different questions, and the difference is the reason
there are two:

  `unsupported_numbers` asks what the model ADDED. It catches the invented
  figure, which is the most damaging fabrication because it is the most
  quotable.

  `missing_protected_terms` asks what the model DROPPED. Nothing in the
  evidence contract can ask this — that contract validates each claim that is
  present and has no notion of what was present before. It only matters where
  the produced text REPLACES the source rather than being derived alongside it,
  which today is the polish stage alone.

Nothing here raises, calls the model, or touches the database. Every function
returns the offending items and lets the caller decide what that costs.
"""

from __future__ import annotations

import re
from typing import List

from service.market_matching import _word_match

# Matches integers, decimals, and percentages. Deliberately loose on what
# precedes/follows so "40%", "3.72", and "200+" all get caught and checked.
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def unsupported_numbers(text: str, evidence_text: str) -> List[str]:
    """Return figures in `text` that do not appear in the cited evidence.

    This is the check that catches the most damaging class of fabrication. A
    model given "helped optimize the checkout flow" will readily produce
    "optimized checkout flow, cutting load time 40%" — plausible, well-phrased,
    and completely invented. The id-resolution checks cannot catch it, because
    the bullet legitimately cites the experience it embellished.

    Matched as substrings of the evidence, not as whole tokens: "3" should count
    as supported when the evidence says "3 credits", and "3.72" when it says
    "3.72/4.00".
    """
    found = NUMBER.findall(text)
    if not found:
        return []
    return [n for n in found if n not in evidence_text]


# A token worth protecting, split off punctuation that merely ends a sentence or
# separates a list. Interior punctuation is kept because it is part of the name:
# "Node.js" and "scikit-learn" are one token, "Python," is not.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+.#\-]*")

# What ends a sentence. The token right after one of these is capitalized by
# grammar rather than by being a name, so rule (b) below skips it.
_SENTENCE_END = re.compile(r"[.!?;:\n]\s*$|^$")


def protected_terms(text: str) -> List[str]:
    """Names in `text` that a rewrite of it must not silently drop.

    A polished description replaces the student's own words, and the replacement
    is later read by `market_matching._check_transferable`, which decides
    whether the student's background supports a skill employers ask for by
    literal word match. A rewrite that turns "Wrote ETL jobs in Python against
    Postgres" into "Built data pipelines" reads better and costs the student two
    verified skills. Nothing downstream can notice: the shorter line is entirely
    truthful, and truthfulness is all the evidence contract checks.

    Two rules, both deliberately narrow:

      (a) a token with an interior capital or an interior ``+ . #`` — "PyTorch",
          "ETL", "Node.js", "C++", "C#". These are names in any position,
          including the start of a sentence.

      (b) a capitalized token that is NOT sentence-initial — "Python" and "SQL"
          in "Used Python and SQL to…", while "Answered" in "Answered support
          tickets" is left alone.

    Both rules under-protect on purpose, in two known places. A name that opens
    a sentence in lower case is missed. And an all-lowercase hyphenated name is
    missed — "scikit-learn" is not protected, because an interior hyphen cannot
    be told from the one in "auto-close" or "on-campus", and treating every
    hyphenated word as a name rejects most honest rewrites.

    That is the right direction to be wrong in. A missed protection costs one
    skill match on one line, and only when the rewrite happens to drop that
    word. A spurious one rejects a good rewrite, and what the student learns is
    that the button does not work.
    """
    terms: List[str] = []
    for match in _TOKEN.finditer(text):
        token = match.group(0).rstrip(".-")
        if not token:
            continue
        interior = token[1:]
        is_name_shaped = any(c.isupper() for c in interior) or any(c in "+.#" for c in interior)
        preceding = text[: match.start()]
        sentence_initial = bool(_SENTENCE_END.search(preceding.rstrip(" \t")) or not preceding.strip())
        if is_name_shaped or (token[0].isupper() and not sentence_initial):
            if token.lower() not in {t.lower() for t in terms}:
                terms.append(token)
    return terms


def missing_protected_terms(produced: str, source: str) -> List[str]:
    """Names in `source` that `produced` dropped, in the order they appeared.

    Checked with `market_matching._word_match` rather than a plain substring
    test so this agrees exactly with the matcher that will later read the
    produced text. If the matcher would not find the term, this must report it
    missing — a laxer check here would pass a rewrite the matcher then fails on.
    """
    return [term for term in protected_terms(source) if not _word_match(produced, term)]
