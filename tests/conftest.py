import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.db import engine


@pytest.fixture()
def db_session():
    """Throwaway-schema fixture: every test gets an isolated Postgres schema."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

    scoped_engine = engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(bind=scoped_engine)

    session = sessionmaker(bind=scoped_engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
