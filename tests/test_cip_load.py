import re

from sqlalchemy import select

from models.cip_code import CIP_CODE_PATTERNS, CipCode
from pipeline.load_cip import DEFAULT_CSV_PATH, parse_csv, upsert_records


def test_csv_parses_without_dropped_rows():
    records = parse_csv(DEFAULT_CSV_PATH)
    assert len(records) > 2000


def test_every_code_matches_its_level_pattern():
    records = parse_csv(DEFAULT_CSV_PATH)
    for r in records:
        assert re.match(CIP_CODE_PATTERNS[r.level], r.cip_code), (r.cip_code, r.level)


def test_no_orphan_parents():
    records = parse_csv(DEFAULT_CSV_PATH)
    all_codes = {r.cip_code for r in records}
    for r in records:
        if r.parent_cip_code is not None:
            assert r.parent_cip_code in all_codes


def test_every_six_digit_code_has_a_four_digit_ancestor():
    records = parse_csv(DEFAULT_CSV_PATH)
    four_digit_codes = {r.cip_code for r in records if r.level == 4}
    for r in records:
        if r.level == 6:
            assert r.parent_cip_code is not None
            assert r.parent_cip_code in four_digit_codes


def test_no_duplicate_codes():
    records = parse_csv(DEFAULT_CSV_PATH)
    codes = [r.cip_code for r in records]
    assert len(codes) == len(set(codes))


def test_loader_is_idempotent(db_session):
    records = parse_csv(DEFAULT_CSV_PATH)

    upsert_records(records, session=db_session)
    first_count = db_session.execute(select(CipCode)).scalars().all()

    upsert_records(records, session=db_session)
    second_count = db_session.execute(select(CipCode)).scalars().all()

    assert len(first_count) == len(records)
    assert len(second_count) == len(records)
