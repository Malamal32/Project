"""Idempotent loader for O*NET occupations and their alternate titles.

Run as: python -m pipeline.load_occupations
Source: data/reference/onet_occupation_data.csv, onet_job_titles.csv,
onet_job_zones.csv (see data/reference/SOURCE.md).
"""

import csv
from pathlib import Path
from typing import Optional

import structlog
import typer
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from models.db import get_session
from models.occupation import (
    Occupation,
    OccupationAltTitle,
    OccupationAltTitleRecord,
    OccupationRecord,
)

log = structlog.get_logger()
app = typer.Typer()

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"
DEFAULT_OCCUPATION_CSV = REFERENCE_DIR / "onet_occupation_data.csv"
DEFAULT_JOB_TITLES_CSV = REFERENCE_DIR / "onet_job_titles.csv"
DEFAULT_JOB_ZONES_CSV = REFERENCE_DIR / "onet_job_zones.csv"


def _soc_2018_code(onet_soc_code: str) -> str:
    """O*NET-SOC codes are SOC 2018 codes plus a .XX detail suffix."""
    return onet_soc_code.split(".")[0]


def parse_occupations(
    occupation_csv: Path, job_zones_csv: Path
) -> list[OccupationRecord]:
    job_zone_by_code: dict[str, int] = {}
    with job_zones_csv.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            job_zone_by_code[row["O*NET-SOC Code"].strip()] = int(row["Job Zone"])

    records: list[OccupationRecord] = []
    with occupation_csv.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            onet_soc_code = row["O*NET-SOC Code"].strip()
            records.append(
                OccupationRecord(
                    onet_soc_code=onet_soc_code,
                    title=row["Title"],
                    description=row.get("Description") or None,
                    soc_2018_code=_soc_2018_code(onet_soc_code),
                    job_zone=job_zone_by_code.get(onet_soc_code),
                    bright_outlook=None,
                )
            )
    return records


def parse_alt_titles(job_titles_csv: Path) -> list[OccupationAltTitleRecord]:
    records: list[OccupationAltTitleRecord] = []
    with job_titles_csv.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            records.append(
                OccupationAltTitleRecord(
                    onet_soc_code=row["O*NET-SOC Code"].strip(),
                    alt_title=row["Job Title"],
                    short_title=row.get("Short Title") or None,
                    source=row.get("Source(s)") or None,
                )
            )
    return records


def upsert_occupations(
    records: list[OccupationRecord], session: Optional[Session] = None
) -> None:
    owns_session = session is None
    session = session or get_session()
    try:
        for record in records:
            stmt = pg_insert(Occupation).values(**record.model_dump())
            stmt = stmt.on_conflict_do_update(
                index_elements=["onet_soc_code"],
                set_={
                    "title": stmt.excluded.title,
                    "description": stmt.excluded.description,
                    "soc_2018_code": stmt.excluded.soc_2018_code,
                    "job_zone": stmt.excluded.job_zone,
                    "bright_outlook": stmt.excluded.bright_outlook,
                },
            )
            session.execute(stmt)
        session.commit()
    finally:
        if owns_session:
            session.close()


def upsert_alt_titles(
    records: list[OccupationAltTitleRecord], session: Optional[Session] = None
) -> None:
    owns_session = session is None
    session = session or get_session()
    try:
        for record in records:
            stmt = pg_insert(OccupationAltTitle).values(**record.model_dump())
            stmt = stmt.on_conflict_do_update(
                index_elements=["onet_soc_code", "alt_title"],
                set_={
                    "short_title": stmt.excluded.short_title,
                    "source": stmt.excluded.source,
                },
            )
            session.execute(stmt)
        session.commit()
    finally:
        if owns_session:
            session.close()


@app.command()
def main(
    occupation_csv: Path = DEFAULT_OCCUPATION_CSV,
    job_titles_csv: Path = DEFAULT_JOB_TITLES_CSV,
    job_zones_csv: Path = DEFAULT_JOB_ZONES_CSV,
) -> None:
    log.info("occupations_load.start")
    occupations = parse_occupations(occupation_csv, job_zones_csv)
    upsert_occupations(occupations)
    log.info("occupations_load.occupations_done", count=len(occupations))

    alt_titles = parse_alt_titles(job_titles_csv)
    upsert_alt_titles(alt_titles)
    log.info("occupations_load.alt_titles_done", count=len(alt_titles))


if __name__ == "__main__":
    app()
