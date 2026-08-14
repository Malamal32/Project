"""Career-page allowlist mechanism (hard constraint 1c): a career-page collector
may only fetch a domain that's been explicitly added to
data/reference/career_page_allowlist.csv. The file ships empty — populate it
before writing/enabling a career-page collector.
"""

from __future__ import annotations

import csv
from pathlib import Path

DEFAULT_ALLOWLIST_PATH = Path(__file__).resolve().parent.parent / "data" / "reference" / "career_page_allowlist.csv"


class NotAllowlistedError(RuntimeError):
    """Raised when a career-page collector targets a domain that isn't allowlisted."""


def load_allowlist(path: Path = DEFAULT_ALLOWLIST_PATH) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row["domain"].strip().lower() for row in reader if row.get("domain", "").strip()}


def is_allowlisted(domain: str, path: Path = DEFAULT_ALLOWLIST_PATH) -> bool:
    return domain.strip().lower() in load_allowlist(path)


def require_allowlisted(domain: str, path: Path = DEFAULT_ALLOWLIST_PATH) -> None:
    if not is_allowlisted(domain, path):
        raise NotAllowlistedError(
            f"{domain!r} is not in {path} — add it before enabling a career-page collector for this domain"
        )
