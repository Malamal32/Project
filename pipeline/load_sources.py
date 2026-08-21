"""Idempotent loader for the sources table (Phase 3 seed: verified Greenhouse
job boards + any not-yet-live licensed-feed stubs).

Run as: python -m pipeline.load_sources
Source: data/reference/sources_seed.csv
"""

import csv
from pathlib import Path
from typing import Optional

import structlog
import typer
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from models.db import get_session
from models.source import Source, SourceRecord

log = structlog.get_logger()
app = typer.Typer()

DEFAULT_CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "reference" / "sources_seed.csv"


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes"}


def parse_csv(csv_path: Path) -> list[SourceRecord]:
    records: list[SourceRecord] = []

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            politeness_config = {}
            if raw_row.get("crawl_delay_seconds"):
                politeness_config["crawl_delay_seconds"] = float(raw_row["crawl_delay_seconds"])
            if raw_row.get("rate_limit_per_minute"):
                politeness_config["rate_limit_per_minute"] = int(raw_row["rate_limit_per_minute"])

            try:
                record = SourceRecord(
                    source_type=raw_row["source_type"],
                    name=raw_row["name"],
                    base_url=raw_row["base_url"],
                    auth_mode=raw_row["auth_mode"],
                    enabled=_parse_bool(raw_row["enabled"]),
                    politeness_config=politeness_config,
                    terms_reviewed_at=raw_row.get("terms_reviewed_at") or None,
                )
            except ValidationError as exc:
                log.warning("load_sources.validation_failed", name=raw_row.get("name"), error=str(exc))
                continue

            records.append(record)

    return records


def upsert_records(records: list[SourceRecord], session: Optional[Session] = None) -> None:
    owns_session = session is None
    session = session or get_session()
    try:
        for record in records:
            stmt = pg_insert(Source).values(**record.model_dump())
            stmt = stmt.on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "source_type": stmt.excluded.source_type,
                    "base_url": stmt.excluded.base_url,
                    "auth_mode": stmt.excluded.auth_mode,
                    "enabled": stmt.excluded.enabled,
                    "politeness_config": stmt.excluded.politeness_config,
                    "terms_reviewed_at": stmt.excluded.terms_reviewed_at,
                },
            )
            session.execute(stmt)
        session.commit()
    finally:
        if owns_session:
            session.close()


@app.command()
def main(csv_path: Path = DEFAULT_CSV_PATH) -> None:
    log.info("load_sources.start", csv_path=str(csv_path))
    records = parse_csv(csv_path)
    log.info("load_sources.parsed", count=len(records))
    upsert_records(records)
    log.info("load_sources.done", count=len(records))


if __name__ == "__main__":
    app()
