"""
Focused regression tests for the real bug reported live: reloading an
episode whose video/publish was genuinely still in progress showed the
Produce/Publish button's elapsed timer reset to 0:00 every time, which
read exactly like clicking the episode had just triggered a fresh
Produce -- because the dashboard's "resume timer on reload" logic used
Date.now() (the reload's own time) instead of a real, persisted start
time, since none existed.

Episode.video_started_at / publish_started_at (new columns, migration
c1d4e8f92a67) fix this: set synchronously the moment each stage
actually starts, cleared when a reprocess resets a stale episode back
to "pending". These tests confirm the write/clear points and that the
API response actually exposes them (the dashboard reads them directly
off the episode object -- see app/dashboard/app.js's wireHeaderButtons).
"""
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base
from app.models import Episode
from app.tasks import episode_video as episode_video_task
from app.tasks.publishing import publish_episode_to_youtube
from app.tasks.ranking import _reprocess_episode


EPISODE_DATE = date(2026, 9, 29)
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _seconds_since(value: datetime) -> float:
    # SQLite (this file's test backend) drops tzinfo on round-trip
    # through a DateTime(timezone=True) column -- real Postgres always
    # preserves it. Treat a naive value as UTC, same defensive pattern
    # already used elsewhere in this codebase (e.g.
    # storyboard_service._storyboard_is_valid).
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - value).total_seconds()


@pytest.fixture
def sqlite_session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    engine = engine.execution_options(schema_translate_map={"raw": None, "editorial": None})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def api_db(sqlite_session_factory):
    session = sqlite_session_factory()
    yield session
    session.close()


def test_produce_episode_video_sets_real_video_started_at(db_session, monkeypatch):
    episode = Episode(episode_date=EPISODE_DATE, status="draft")
    db_session.add(episode)
    db_session.commit()

    db_session.close = lambda: None
    monkeypatch.setattr(episode_video_task, "SessionLocal", lambda: db_session)
    # Avoid real rendering entirely -- this test is only about the
    # video_started_at write, already covered by other tests for the
    # rendering itself.
    monkeypatch.setattr(
        "app.content.episode_renderer.render_episode",
        lambda episode_id: (_ for _ in ()).throw(RuntimeError("stop before rendering")),
    )

    episode_video_task.produce_episode_video(episode.id)

    db_session.refresh(episode)
    assert episode.video_started_at is not None
    # Real, current time -- not a placeholder/epoch value.
    assert _seconds_since(episode.video_started_at) < 30


def test_trigger_episode_publish_sets_real_publish_started_at(monkeypatch, sqlite_session_factory, api_db):
    monkeypatch.setattr(main, "SessionLocal", sqlite_session_factory)
    episode = Episode(
        episode_date=EPISODE_DATE, status="approved", video_status="ready",
        publish_status="not_published",
    )
    api_db.add(episode)
    api_db.commit()
    monkeypatch.setattr(publish_episode_to_youtube, "delay", MagicMock(return_value=MagicMock(id="fake-task-id")))

    main.trigger_episode_publish(episode.id)

    api_db.refresh(episode)
    assert episode.publish_status == "publishing"
    assert episode.publish_started_at is not None
    assert _seconds_since(episode.publish_started_at) < 30


def test_reprocess_clears_video_started_at(db_session):
    episode = Episode(
        episode_date=EPISODE_DATE, status="draft", video_status="producing",
        video_started_at=NOW,
    )
    db_session.add(episode)
    db_session.commit()

    _reprocess_episode(db_session, episode.id, datetime.now(timezone.utc))

    db_session.refresh(episode)
    assert episode.video_status == "pending"
    assert episode.video_started_at is None


def test_serialized_episode_exposes_started_at_fields(db_session):
    episode = Episode(
        episode_date=EPISODE_DATE, status="draft", video_status="producing",
        video_started_at=NOW, publish_status="publishing", publish_started_at=NOW,
    )
    db_session.add(episode)
    db_session.commit()

    result = main._serialize_episode(db_session, episode)

    assert result["video_started_at"] == NOW
    assert result["publish_started_at"] == NOW
