"""Coverage for the FTS5 index that replaced the Postgres pg_trgm GIN index.

Nothing consumes this yet — it is the Phase 4 tier-2 (fuzzy title match) substrate.
It is tested now because the index is built by a pipeline stage rather than by the
ORM, so nothing else would catch it silently going missing or going stale.
"""

from __future__ import annotations

from sqlalchemy import text

from models.occupation import Occupation, OccupationAltTitle
from pipeline.load_occupations import rebuild_alt_title_fts


def _seed(db_session) -> None:
    db_session.add_all([
        Occupation(
            onet_soc_code="15-1252.00",
            title="Software Developers",
            soc_2018_code="15-1252",
        ),
        Occupation(
            onet_soc_code="29-1141.00",
            title="Registered Nurses",
            soc_2018_code="29-1141",
        ),
    ])
    db_session.flush()
    db_session.add_all([
        OccupationAltTitle(onet_soc_code="15-1252.00", alt_title="Software Engineer"),
        OccupationAltTitle(onet_soc_code="15-1252.00", alt_title="Applications Developer"),
        OccupationAltTitle(onet_soc_code="29-1141.00", alt_title="Staff Nurse"),
    ])
    db_session.commit()


def _match(db_session, query: str) -> list[str]:
    rows = db_session.execute(
        text(
            "SELECT onet_soc_code FROM occupation_alt_titles_fts "
            "WHERE occupation_alt_titles_fts MATCH :q ORDER BY rank"
        ),
        {"q": query},
    ).scalars().all()
    return rows


def test_rebuild_indexes_every_alt_title(db_session):
    _seed(db_session)
    assert rebuild_alt_title_fts(db_session) == 3


def test_match_finds_the_right_occupation(db_session):
    _seed(db_session)
    rebuild_alt_title_fts(db_session)

    assert _match(db_session, "software engineer") == ["15-1252.00"]
    assert _match(db_session, "nurse") == ["29-1141.00"]


def test_match_is_case_insensitive_and_token_based(db_session):
    _seed(db_session)
    rebuild_alt_title_fts(db_session)

    # Token order and case are irrelevant to FTS5; this is the capability that
    # replaces trigram search, not an exact-string index.
    assert _match(db_session, "DEVELOPER") == ["15-1252.00"]
    assert _match(db_session, '"applications developer"') == ["15-1252.00"]


def test_rebuild_is_idempotent_and_drops_removed_titles(db_session):
    _seed(db_session)
    rebuild_alt_title_fts(db_session)

    db_session.execute(text("DELETE FROM occupation_alt_titles WHERE alt_title = 'Staff Nurse'"))
    db_session.commit()

    assert rebuild_alt_title_fts(db_session) == 2
    assert _match(db_session, "nurse") == []
