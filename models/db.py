import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

DEFAULT_DATABASE_URL = "sqlite+pysqlite:///./data/hiring_db.sqlite3"
DATABASE_URL = os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL

engine = create_engine(DATABASE_URL, future=True)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """SQLite ignores foreign keys unless asked not to, and D1 enforces them. Without
    this the local database would happily accept rows that D1 later rejects, so the
    pragma is what keeps `sync_d1` from being the first place a violation shows up.

    WAL is for concurrent reads during long ingestion runs; it is a local-file
    concern only and has no bearing on what ships to D1.
    """
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, future=True)


def get_session() -> Session:
    return SessionLocal()
