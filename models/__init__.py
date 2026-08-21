"""Importing this package registers every mapped class on `Base.metadata`.

Several things need the *complete* schema, not just the tables a caller happens to
have imported: the D1 DDL generator, the schema-parity test, and the D1 sync stage.
Alembic's `env.py` used to carry this import list; it lives here now so there is one
place that knows the full set of tables.

`models.db` is deliberately not imported — it constructs an engine at import time,
and metadata consumers should not need a database connection.
"""

from models.base import Base
from models.cip_code import CipCode
from models.company import Company
from models.occupation import CipSocCrosswalk, Occupation, OccupationAltTitle
from models.posting import Posting, PostingVersion
from models.raw_document import RawDocument
from models.source import Source
from models.student import StudentAttribute, StudentCourse, StudentProfile

__all__ = [
    "Base",
    "CipCode",
    "CipSocCrosswalk",
    "Company",
    "Occupation",
    "OccupationAltTitle",
    "Posting",
    "PostingVersion",
    "RawDocument",
    "Source",
    "StudentAttribute",
    "StudentCourse",
    "StudentProfile",
]
