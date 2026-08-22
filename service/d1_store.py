"""Cloudflare D1 backends for the service, used only by the deployed Worker.

The pipeline reaches D1 through `wrangler d1 execute` and the local SQLite
mirror; the Worker reaches the *same database* through a binding, which is a
different API entirely — `env.DB.prepare(sql).bind(...)` rather than SQLAlchemy.
This module is that second path, and it exists only here: nothing under
`pipeline/` or `models/` imports it, and it is never imported when the service
runs locally.

Two backends, both thin:

- `D1ProfileStore` writes a reviewed profile. It is registered with
  `service.profile_store.use_backend()` at Worker startup, which is what makes
  `POST /api/student/profile` work on the edge.
- `D1RoleSearch` reads the O*NET occupation index the pipeline already
  published, which is what turns the frontend's mocked role autocomplete into a
  real query.

Why raw SQL and not the ORM: D1's binding speaks prepared statements, and
SQLAlchemy has no dialect for it. The DDL both ends share still comes from
`migrations/d1/*.sql`, so the column names below are checked by the same
`tests/test_schema_parity.py` that guards the ORM.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, List, Tuple

import structlog

from models.student import StudentProfileRecord

log = structlog.get_logger()


def _now() -> str:
    """D1 stores TZDateTime as an ISO string; `models/types.py` writes the same
    shape from the pipeline side, so both ends read back identically."""
    return datetime.now(timezone.utc).isoformat()


def _uuid_hex() -> str:
    """`sa.Uuid` renders as CHAR(32) — hex, no dashes. Matching that here is
    what lets a Worker-written row join against a pipeline-written one."""
    return uuid.uuid4().hex


class D1ProfileStore:
    """Writes `student_profiles` + `student_courses` + `student_attributes`.

    Mirrors `service.profile_store.save_profile` row for row. The two must agree
    on column names and on the fields-stored-verbatim rule; they are checked
    against the same migration.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    async def save(self, record: StudentProfileRecord) -> Tuple[uuid.UUID, int, int]:
        profile_id = _uuid_hex()
        now = _now()

        statements = [
            self._db.prepare(
                """
                INSERT INTO student_profiles (
                    student_profile_id, institution, degree, degree_level, major,
                    minor, concentration, graduation_date, expected_graduation_date,
                    gpa, extraction_method, model_version, created_at, updated_at
                ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14)
                """
            ).bind(
                profile_id,
                record.institution,
                record.degree,
                record.degree_level,
                record.major,
                record.minor,
                record.concentration,
                record.graduation_date,
                record.expected_graduation_date,
                record.gpa,
                record.extraction_method,
                record.model_version,
                now,
                now,
            )
        ]

        for course in record.courses:
            statements.append(
                self._db.prepare(
                    """
                    INSERT INTO student_courses (
                        student_course_id, student_profile_id, course_code, course_name,
                        credits, grade, term, student_approved, position
                    ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)
                    """
                ).bind(
                    _uuid_hex(),
                    profile_id,
                    course.course_code,
                    course.course_name,
                    course.credits,
                    course.grade,
                    course.term,
                    1 if course.student_approved else 0,
                    course.position,
                )
            )

        attribute_count = 0
        for attribute_type, values in (
            ("skill", record.skills),
            ("honor", record.honors),
            ("certification", record.certifications),
        ):
            for position, value in enumerate(values):
                if not value:
                    continue
                attribute_count += 1
                statements.append(
                    self._db.prepare(
                        """
                        INSERT INTO student_attributes (
                            student_attribute_id, student_profile_id, attribute_type,
                            value, position
                        ) VALUES (?1,?2,?3,?4,?5)
                        """
                    ).bind(_uuid_hex(), profile_id, attribute_type, value, position)
                )

        # `batch` is D1's atomic unit — the whole list commits or none of it
        # does. This is the closest thing D1 offers to the transaction the
        # SQLAlchemy path opens, and it is why the inserts are built up first
        # rather than issued as they are constructed.
        await self._db.batch(statements)

        log.info(
            "d1_store.saved",
            student_profile_id=profile_id,
            extraction_method=record.extraction_method,
            model_version=record.model_version,
            courses=len(record.courses),
            attributes=attribute_count,
        )
        return uuid.UUID(profile_id), len(record.courses), attribute_count


class D1RoleSearch:
    """Autocomplete over the O*NET occupation index the pipeline published.

    Two tiers, in the order the classification cascade uses them (PROMPT.md
    Phase 4): exact-ish prefix match on `occupations.title` first, then the
    alternate-title index. Alternate titles are where the recall is — 57k of
    them against 1k occupations — so a student typing "coder" or "programmer"
    finds Software Developers even though neither is the official title.

    Results are capped and de-duplicated by SOC code: an occupation reachable
    through five alternate titles should appear once, not five times.
    """

    def __init__(self, db: Any, limit: int = 8) -> None:
        self._db = db
        self._limit = limit

    async def search(self, query: str) -> List[dict]:
        term = (query or "").strip()
        if len(term) < 2:
            return []

        # LIKE with an escaped pattern rather than FTS5: the FTS index is built
        # over alt titles for whole-word lookup, and a *prefix* match while the
        # student is still typing is a different query. Wildcards in the term
        # are escaped so a typed "%" doesn't turn into match-everything.
        pattern = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        rows = await self._db.prepare(
            """
            SELECT onet_soc_code, title, source, MIN(rank) AS rank FROM (
                SELECT onet_soc_code, title, 'title' AS source, 0 AS rank
                  FROM occupations
                 WHERE title LIKE ?1 ESCAPE '\\'
                UNION ALL
                SELECT o.onet_soc_code, o.title, 'alt_title' AS source, 1 AS rank
                  FROM occupation_alt_titles a
                  JOIN occupations o ON o.onet_soc_code = a.onet_soc_code
                 WHERE a.alt_title LIKE ?1 ESCAPE '\\'
            )
            GROUP BY onet_soc_code, title
            ORDER BY rank, title
            LIMIT ?2
            """
        ).bind(pattern, self._limit).all()

        results = getattr(rows, "results", None) or []
        return [
            {
                "display_name": row["title"],
                "onet_soc_code": row["onet_soc_code"],
                # Which tier matched. Surfaced so the UI *could* distinguish an
                # official title from a colloquial one; it currently does not.
                "matched_on": row["source"],
            }
            for row in results
        ]
