"""Idempotent loader for the NCES CIP 2020 code list.

Run as: python -m pipeline.load_cip
Source: data/reference/CIPCode2020.csv (see data/reference/SOURCE.md).
"""

import csv
import re
from pathlib import Path
from typing import Optional

import structlog
import typer
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from models.cip_code import CipCode, CipCodeRecord
from models.db import get_session

log = structlog.get_logger()
app = typer.Typer()

DEFAULT_CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "reference" / "CIPCode2020.csv"

LEVEL_PATTERNS = [
    (re.compile(r"^\d{2}$"), 2),
    (re.compile(r"^\d{2}\.\d{2}$"), 4),
    (re.compile(r"^\d{2}\.\d{4}$"), 6),
]

# The NCES export's Action column marks which CIP2020 rows are the current,
# citable code vs. a deprecated stub kept only for CIP2010->CIP2020 reconciliation.
INACTIVE_ACTIONS = {"Moved from", "Deleted"}


def _strip_excel_formula_wrapper(value: str) -> str:
    """NCES exports CIP codes as ="01" / ="01.0101" to preserve leading zeros in Excel."""
    value = value.strip()
    if value.startswith('="') and value.endswith('"'):
        return value[2:-1]
    return value


def _classify_level(cip_code: str) -> Optional[int]:
    for pattern, level in LEVEL_PATTERNS:
        if pattern.match(cip_code):
            return level
    return None


def _parent_of(cip_code: str, level: int) -> Optional[str]:
    if level == 4:
        return cip_code.split(".")[0]
    if level == 6:
        return cip_code[:5]
    return None


def parse_csv(csv_path: Path) -> list[CipCodeRecord]:
    records: list[CipCodeRecord] = []
    seen_codes: set[str] = set()

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            cip_code = _strip_excel_formula_wrapper(raw_row["CIPCode"])
            level = _classify_level(cip_code)
            if level is None:
                log.warning("cip_load.unrecognized_format", cip_code=cip_code)
                continue

            action = (raw_row.get("Action") or "").strip()
            parent_cip_code = _parent_of(cip_code, level)

            try:
                record = CipCodeRecord(
                    cip_code=cip_code,
                    cip_title=raw_row["CIPTitle"].strip(),
                    cip_definition=raw_row.get("CIPDefinition") or None,
                    level=level,
                    parent_cip_code=parent_cip_code,
                    is_active=action not in INACTIVE_ACTIONS,
                    crosswalk_notes=action or None,
                )
            except ValidationError as exc:
                log.warning("cip_load.validation_failed", cip_code=cip_code, error=str(exc))
                continue

            seen_codes.add(cip_code)
            records.append(record)

    all_codes = {r.cip_code for r in records}
    orphans = [r for r in records if r.parent_cip_code and r.parent_cip_code not in all_codes]
    for orphan in orphans:
        log.warning(
            "cip_load.orphan_parent",
            cip_code=orphan.cip_code,
            missing_parent=orphan.parent_cip_code,
        )
    if orphans:
        # Drop the dangling FK rather than fail the whole load; surfaced above.
        orphan_codes = {o.cip_code for o in orphans}
        for r in records:
            if r.cip_code in orphan_codes:
                r.parent_cip_code = None

    return records


def upsert_records(records: list[CipCodeRecord], session: Optional[Session] = None) -> None:
    # Insert in level order (2 -> 4 -> 6) so self-referential FKs always resolve.
    by_level = sorted(records, key=lambda r: r.level)

    owns_session = session is None
    session = session or get_session()
    try:
        for record in by_level:
            stmt = pg_insert(CipCode).values(**record.model_dump())
            stmt = stmt.on_conflict_do_update(
                index_elements=["cip_code"],
                set_={
                    "cip_title": stmt.excluded.cip_title,
                    "cip_definition": stmt.excluded.cip_definition,
                    "level": stmt.excluded.level,
                    "parent_cip_code": stmt.excluded.parent_cip_code,
                    "is_active": stmt.excluded.is_active,
                    "crosswalk_notes": stmt.excluded.crosswalk_notes,
                },
            )
            session.execute(stmt)
        session.commit()
    finally:
        if owns_session:
            session.close()


@app.command()
def main(csv_path: Path = DEFAULT_CSV_PATH) -> None:
    log.info("cip_load.start", csv_path=str(csv_path))
    records = parse_csv(csv_path)
    log.info("cip_load.parsed", count=len(records))
    upsert_records(records)
    log.info("cip_load.done", count=len(records))


if __name__ == "__main__":
    app()
