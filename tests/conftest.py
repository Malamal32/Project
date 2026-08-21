import io

import pytest
from reportlab.pdfgen import canvas
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from pipeline import raw_store
from pipeline.init_db import apply_migrations


@pytest.fixture()
def db_session(tmp_path):
    """Throwaway-database fixture: every test gets its own SQLite file.

    The schema comes from `migrations/d1/*.sql` — the exact DDL that ships to D1 —
    rather than `Base.metadata.create_all()`. That means tests exercise the real
    constraints and the FTS5 virtual table, and a model change that never made it
    into a migration fails here instead of at deploy time.

    Foreign keys are switched on to match D1, which enforces them by default; SQLite
    would otherwise accept rows D1 rejects.
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.sqlite3'}", future=True)

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    raw = engine.raw_connection()
    try:
        apply_migrations(raw.driver_connection)
    finally:
        raw.close()

    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def local_raw_store(tmp_path, monkeypatch):
    """Point the raw-payload store at a temp directory for every test.

    Without this, a test that lands a raw document would either reach for real R2
    credentials or scribble into the repo's `data/raw_store/`. The real `get_store`
    is left in place — only its root and the environment it reads are redirected —
    so tests exercise the actual backend-selection path.
    """
    monkeypatch.setattr(raw_store, "_LOCAL_STORE_DIR", tmp_path / "raw_store")
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    raw_store.get_store.cache_clear()
    yield
    raw_store.get_store.cache_clear()


# --- Transcript extraction service ----------------------------------------


@pytest.fixture(autouse=True)
def _no_live_api_calls(monkeypatch):
    """No test may reach the Anthropic API.

    With the credentials removed, `llm_extraction.is_enabled()` is False and the
    transcript pipeline takes the rule-based path. Tests that exercise the LLM
    stage inject a fake client instead of a key.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def _pdf_with_text(lines: list[str]) -> bytes:
    """Build a PDF with a real text layer, so no student document ever has to be
    checked into the repo to test extraction."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 780
    for line in lines:
        c.drawString(50, y, line)
        y -= 16
        if y < 40:
            c.showPage()
            y = 780
    c.save()
    return buf.getvalue()


@pytest.fixture
def synthetic_transcript_pdf() -> bytes:
    return _pdf_with_text(
        [
            "Riverbend State University",
            "Official Academic Transcript",
            "Bachelor of Science",
            "Major: Computer Science",
            "Minor: Mathematics",
            "Expected Graduation: May 2027",
            "GPA: 3.72/4.00",
            "",
            "## Coursework",
            "CS 310 - Data Structures and Algorithms - 3 cr - Grade: A - Fall 2025",
            "CS 340 - Database Systems - 3 cr - Grade: A- - Spring 2025",
            "MATH 251 - Statistics I - 3 cr - Grade: B+ - Fall 2024",
            "",
            "## Honors",
            "- Dean's List, Fall 2025",
            "",
            "## Certifications",
            "- AWS Cloud Practitioner",
        ]
    )


@pytest.fixture
def synthetic_minimal_pdf() -> bytes:
    # Deliberately sparse: exercises the "couldn't confidently identify" warnings.
    return _pdf_with_text(["Some Document", "No structured academic fields here."])


@pytest.fixture
def synthetic_blank_pdf() -> bytes:
    # No text layer at all -> should be treated like a scanned/image-only PDF.
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(100, 100, 10, 10)  # a shape, no text
    c.save()
    return buf.getvalue()


@pytest.fixture
def synthetic_encrypted_pdf(synthetic_transcript_pdf: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(synthetic_transcript_pdf))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password="secret", owner_password="secret")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
