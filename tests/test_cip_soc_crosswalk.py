from collections import Counter

from sqlalchemy import select

from models.occupation import CipSocCrosswalk
from pipeline.load_cip import DEFAULT_CSV_PATH as CIP_CSV_PATH
from pipeline.load_cip import parse_csv as parse_cip
from pipeline.load_cip import upsert_records as upsert_cip
from pipeline.load_cip_soc_crosswalk import (
    DEFAULT_CROSSWALK_XLSX,
    DEFAULT_OCCUPATION_CSV,
    parse_crosswalk,
    upsert_crosswalk,
)
from pipeline.load_occupations import (
    DEFAULT_JOB_ZONES_CSV,
    parse_occupations,
    upsert_occupations,
)


def test_crosswalk_is_many_to_many_in_both_directions():
    records = parse_crosswalk(DEFAULT_CROSSWALK_XLSX, DEFAULT_OCCUPATION_CSV)

    cip_to_soc_counts = Counter(r.cip_code for r in records)
    soc_to_cip_counts = Counter(r.onet_soc_code for r in records)

    assert any(count > 1 for count in cip_to_soc_counts.values()), (
        "expected at least one CIP code to map to more than one SOC"
    )
    assert any(count > 1 for count in soc_to_cip_counts.values()), (
        "expected at least one SOC code to map to more than one CIP"
    )


def test_crosswalk_loader_is_idempotent(db_session):
    upsert_cip(parse_cip(CIP_CSV_PATH), session=db_session)
    upsert_occupations(
        parse_occupations(DEFAULT_OCCUPATION_CSV, DEFAULT_JOB_ZONES_CSV),
        session=db_session,
    )

    records = parse_crosswalk(DEFAULT_CROSSWALK_XLSX, DEFAULT_OCCUPATION_CSV)

    upsert_crosswalk(records, session=db_session)
    first_count = len(db_session.execute(select(CipSocCrosswalk)).scalars().all())

    upsert_crosswalk(records, session=db_session)
    second_count = len(db_session.execute(select(CipSocCrosswalk)).scalars().all())

    assert first_count == second_count
    assert first_count > 0
