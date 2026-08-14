"""Idempotent loader for the NCES CIP2020-SOC2018 crosswalk.

Run as: python -m pipeline.load_cip_soc_crosswalk
Source: data/reference/CIP2020_SOC2018_Crosswalk.xlsx (see data/reference/SOURCE.md).

The source crosswalk operates at SOC 2018 (6-digit) granularity; this loader fans
each (cip_code, soc_2018_code) pair out to every occupations row sharing that
soc_2018_code, producing the (cip_code, onet_soc_code) pairs the schema expects.
"""

import csv
from pathlib import Path
from typing import Optional

import openpyxl
import structlog
import typer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from models.cip_code import CipCode
from models.db import get_session
from models.occupation import CipSocCrosswalk, CipSocCrosswalkRecord, Occupation

log = structlog.get_logger()
app = typer.Typer()

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"
DEFAULT_CROSSWALK_XLSX = REFERENCE_DIR / "CIP2020_SOC2018_Crosswalk.xlsx"
DEFAULT_OCCUPATION_CSV = REFERENCE_DIR / "onet_occupation_data.csv"

CROSSWALK_SOURCE = "nces_cip_soc_2020"
# Matches the "Retrieved at" timestamp for CIP2020_SOC2018_Crosswalk.xlsx in
# data/reference/SOURCE.md — fixed so re-running the loader against the same
# checked-in file is a true no-op, not a fresh "now" each time.
SOURCE_RETRIEVED_AT = "2026-08-12T09:56:26Z"


def _soc_2018_code(onet_soc_code: str) -> str:
    return onet_soc_code.split(".")[0]


def parse_crosswalk(
    crosswalk_xlsx: Path, occupation_csv: Path
) -> list[CipSocCrosswalkRecord]:
    onet_codes_by_soc2018: dict[str, list[str]] = {}
    with occupation_csv.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            onet_soc_code = row["O*NET-SOC Code"].strip()
            onet_codes_by_soc2018.setdefault(_soc_2018_code(onet_soc_code), []).append(
                onet_soc_code
            )

    wb = openpyxl.load_workbook(crosswalk_xlsx, read_only=True, data_only=True)
    ws = wb["CIP-SOC"]
    rows = list(ws.iter_rows(values_only=True))[1:]  # skip header

    records: list[CipSocCrosswalkRecord] = []
    seen: set[tuple[str, str]] = set()
    for cip_code, _cip_title, soc_2018_code, _soc_title in rows:
        cip_code = str(cip_code).strip()
        soc_2018_code = str(soc_2018_code).strip()

        onet_codes = onet_codes_by_soc2018.get(soc_2018_code)
        if not onet_codes:
            log.warning(
                "cip_soc_crosswalk_load.unmatched_soc",
                soc_2018_code=soc_2018_code,
                cip_code=cip_code,
            )
            continue

        for onet_soc_code in onet_codes:
            key = (cip_code, onet_soc_code)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                CipSocCrosswalkRecord(
                    cip_code=cip_code,
                    onet_soc_code=onet_soc_code,
                    crosswalk_source=CROSSWALK_SOURCE,
                    retrieved_at=SOURCE_RETRIEVED_AT,
                )
            )
    return records


def upsert_crosswalk(
    records: list[CipSocCrosswalkRecord], session: Optional[Session] = None
) -> None:
    owns_session = session is None
    session = session or get_session()
    try:
        valid_onet_codes = {
            r[0] for r in session.execute(select(Occupation.onet_soc_code)).all()
        }
        valid_cip_codes = {r[0] for r in session.execute(select(CipCode.cip_code)).all()}

        dropped = [
            r
            for r in records
            if r.onet_soc_code not in valid_onet_codes or r.cip_code not in valid_cip_codes
        ]
        if dropped:
            log.warning("cip_soc_crosswalk_load.dropped_missing_fk_target", count=len(dropped))
        records = [
            r
            for r in records
            if r.onet_soc_code in valid_onet_codes and r.cip_code in valid_cip_codes
        ]

        for record in records:
            stmt = pg_insert(CipSocCrosswalk).values(**record.model_dump())
            stmt = stmt.on_conflict_do_update(
                index_elements=["cip_code", "onet_soc_code", "crosswalk_source"],
                set_={"retrieved_at": stmt.excluded.retrieved_at},
            )
            session.execute(stmt)
        session.commit()
    finally:
        if owns_session:
            session.close()


@app.command()
def main(
    crosswalk_xlsx: Path = DEFAULT_CROSSWALK_XLSX,
    occupation_csv: Path = DEFAULT_OCCUPATION_CSV,
) -> None:
    log.info("cip_soc_crosswalk_load.start")
    records = parse_crosswalk(crosswalk_xlsx, occupation_csv)
    log.info("cip_soc_crosswalk_load.parsed", count=len(records))
    upsert_crosswalk(records)
    log.info("cip_soc_crosswalk_load.done", count=len(records))


if __name__ == "__main__":
    app()
