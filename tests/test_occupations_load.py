from sqlalchemy import select

from models.occupation import Occupation, OccupationAltTitle
from pipeline.load_occupations import (
    DEFAULT_JOB_TITLES_CSV,
    DEFAULT_JOB_ZONES_CSV,
    DEFAULT_OCCUPATION_CSV,
    parse_alt_titles,
    parse_occupations,
    upsert_alt_titles,
    upsert_occupations,
)


def test_occupations_parse_one_row_per_source_row():
    records = parse_occupations(DEFAULT_OCCUPATION_CSV, DEFAULT_JOB_ZONES_CSV)
    assert len(records) > 900


def test_soc_2018_code_is_derived_correctly():
    records = parse_occupations(DEFAULT_OCCUPATION_CSV, DEFAULT_JOB_ZONES_CSV)
    by_code = {r.onet_soc_code: r for r in records}
    assert by_code["11-1011.00"].soc_2018_code == "11-1011"
    assert by_code["11-1011.03"].soc_2018_code == "11-1011"


def test_alt_titles_have_no_duplicate_natural_key():
    records = parse_alt_titles(DEFAULT_JOB_TITLES_CSV)
    keys = [(r.onet_soc_code, r.alt_title) for r in records]
    assert len(keys) == len(set(keys))


def test_occupations_and_alt_titles_loader_is_idempotent(db_session):
    occupations = parse_occupations(DEFAULT_OCCUPATION_CSV, DEFAULT_JOB_ZONES_CSV)
    alt_titles = parse_alt_titles(DEFAULT_JOB_TITLES_CSV)[:500]  # keep the test fast

    upsert_occupations(occupations, session=db_session)
    upsert_alt_titles(alt_titles, session=db_session)
    first_occ = len(db_session.execute(select(Occupation)).scalars().all())
    first_alt = len(db_session.execute(select(OccupationAltTitle)).scalars().all())

    upsert_occupations(occupations, session=db_session)
    upsert_alt_titles(alt_titles, session=db_session)
    second_occ = len(db_session.execute(select(Occupation)).scalars().all())
    second_alt = len(db_session.execute(select(OccupationAltTitle)).scalars().all())

    assert first_occ == second_occ == len(occupations)
    assert first_alt == second_alt == len(alt_titles)
