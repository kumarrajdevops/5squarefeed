import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base


@pytest.fixture
def db_session():
    """
    A real SQLAlchemy session backed by an in-memory SQLite database,
    for testing functions that take `db` as an explicit parameter
    (e.g. app.tasks.episode_video._produce_story_content) without
    touching the real Postgres database.

    Deliberately not the app's own Postgres engine -- every model in
    app/models.py uses portable column types (String/Integer/Float/
    DateTime/Text/ForeignKey/UniqueConstraint), so SQLite is a faithful
    stand-in and keeps the test suite dependency-free (no running
    Postgres required to run `pytest`). SQLAlchemy raises the same
    IntegrityError on a unique-constraint violation regardless of
    backend, which is what the ingestion race-condition test relies on.

    schema_translate_map maps the real `raw`/`editorial` Postgres
    schemas (app/models.py's __table_args__) to no schema at all for
    this SQLite engine -- SQLite has no real multi-schema support the
    way Postgres does. This is test-only: real Postgres always uses
    the actual `raw`/`editorial` schema names untouched; nothing about
    the production schema is weakened to make SQLite tests easier.
    """

    engine = create_engine("sqlite:///:memory:")
    engine = engine.execution_options(schema_translate_map={"raw": None, "editorial": None})
    Base.metadata.create_all(engine)

    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = SessionLocal()

    try:
        yield session
    finally:
        session.close()
        engine.dispose()
