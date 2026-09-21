import json
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base
from app.models import Episode, EpisodeStory, NewsItem, StoryState
from app.tasks.ranking import (
    _classify_episode_lookup,
    _create_episode_or_recover,
    _reprocess_episode,
    _run_ranking_selection,
    reprocess_episode,
)
from app.tasks.scheduled import run_daily_processing


EPISODE_DATE = date(2026, 9, 22)

# Naive, not timezone.utc-aware: SQLite (this fixture's backend) drops
# tzinfo on round-trip through a DateTime(timezone=True) column, so a
# NewsItem read back via db_session has a naive published_at regardless
# of what was written. compute_total_score() subtracts `now` from
# published_at, which raises TypeError if one side is aware and the
# other naive. Real production code is unaffected -- it always runs
# against Postgres, which preserves tzinfo faithfully, and always
# passes datetime.now(timezone.utc) for `now`; this naive value exists
# only to match what this SQLite fixture actually gives back in tests.
NOW = datetime(2026, 9, 22, 12, 0)


def _insert_story(db, url, published_at=None, collection_date=EPISODE_DATE):
    """
    Inserts a matching raw.NewsItem + editorial.StoryState pair --
    ranking's eligibility query (app/tasks/ranking.py) joins the two,
    scoped to NewsItem.collection_date == episode_date. Returns the
    NewsItem (its .id is the shared identity both tables use).
    """
    item = NewsItem(
        title="Some story",
        canonical_url=url,
        source_name="Example Source",
        source_type="rss",
        published_at=published_at or NOW,
        collected_at=NOW,
        collection_date=collection_date,
        status="collected",
    )
    db.add(item)
    db.flush()

    db.add(StoryState(
        id=item.id,
        ai_relevance="ai_candidate",
        ai_relevance_score=0.8,
    ))
    db.commit()
    return item


# ---------------------------------------------------------
# _classify_episode_lookup -- pure function, no DB needed. Also the
# only place "multiple existing episodes" can be exercised at all:
# uq_editorial_episodes_episode_date makes that state impossible to
# reproduce via real inserts in any properly-constrained database
# (test or prod) going forward -- see the function's own docstring.
# ---------------------------------------------------------

class _FakeEpisode:
    def __init__(self, id, status="draft", publish_status="not_published"):
        self.id = id
        self.status = status
        self.publish_status = publish_status


def test_classify_no_existing_episodes_returns_none():
    assert _classify_episode_lookup([], EPISODE_DATE) is None


def test_classify_multiple_existing_episodes_lists_all_candidates():
    result = _classify_episode_lookup([_FakeEpisode(1), _FakeEpisode(2)], EPISODE_DATE)
    assert result == {
        "episode_date": EPISODE_DATE.isoformat(),
        "created": False,
        "reason": "multiple_existing_episodes",
        "candidate_episode_ids": [1, 2],
    }


def test_classify_existing_draft_is_reused():
    result = _classify_episode_lookup([_FakeEpisode(5, status="draft")], EPISODE_DATE)
    assert result == {
        "episode_id": 5,
        "episode_date": EPISODE_DATE.isoformat(),
        "created": False,
        "reason": "existing_draft_reused",
    }


def test_classify_existing_rejected_is_blocked():
    result = _classify_episode_lookup([_FakeEpisode(5, status="rejected")], EPISODE_DATE)
    assert result["reason"] == "episode_rejected"


def test_classify_existing_approved_is_blocked():
    result = _classify_episode_lookup([_FakeEpisode(5, status="approved")], EPISODE_DATE)
    assert result["reason"] == "episode_approved"


def test_classify_existing_published_takes_priority_over_approved():
    result = _classify_episode_lookup(
        [_FakeEpisode(5, status="approved", publish_status="published")], EPISODE_DATE
    )
    assert result["reason"] == "episode_published"


# ---------------------------------------------------------
# _run_ranking_selection -- real SQLite DB via the shared db_session
# fixture (tests/conftest.py), which creates the same
# uq_editorial_episodes_episode_date constraint since it comes from
# the real model metadata.
# ---------------------------------------------------------

def test_run_ranking_selection_creates_when_none_exists(db_session):
    _insert_story(db_session, "https://example.com/a")

    result = _run_ranking_selection(db_session, EPISODE_DATE, NOW)

    assert result["created"] is True
    episode = db_session.get(Episode, result["episode_id"])
    assert episode.episode_date == EPISODE_DATE
    assert episode.status == "draft"
    snapshot = (
        db_session.query(EpisodeStory)
        .filter(EpisodeStory.episode_id == episode.id)
        .all()
    )
    assert len(snapshot) == 1


def test_run_ranking_selection_reuses_existing_draft_without_touching_snapshot(db_session):
    _insert_story(db_session, "https://example.com/a")
    first = _run_ranking_selection(db_session, EPISODE_DATE, NOW)
    assert first["created"] is True

    before = sorted(
        (es.id, es.story_id) for es in
        db_session.query(EpisodeStory)
        .filter(EpisodeStory.episode_id == first["episode_id"])
        .all()
    )

    # A brand new eligible story appears before the second call -- if
    # selection silently re-ran, it would show up in the snapshot.
    _insert_story(db_session, "https://example.com/b")

    second = _run_ranking_selection(db_session, EPISODE_DATE, NOW)

    assert second == {
        "episode_id": first["episode_id"],
        "episode_date": EPISODE_DATE.isoformat(),
        "created": False,
        "reason": "existing_draft_reused",
    }
    after = sorted(
        (es.id, es.story_id) for es in
        db_session.query(EpisodeStory)
        .filter(EpisodeStory.episode_id == first["episode_id"])
        .all()
    )
    assert after == before
    assert db_session.query(Episode).filter(Episode.episode_date == EPISODE_DATE).count() == 1


def test_run_ranking_selection_blocks_on_rejected(db_session):
    episode = Episode(episode_date=EPISODE_DATE, status="rejected")
    db_session.add(episode)
    db_session.commit()

    result = _run_ranking_selection(db_session, EPISODE_DATE, NOW)

    assert result == {
        "episode_id": episode.id,
        "episode_date": EPISODE_DATE.isoformat(),
        "created": False,
        "reason": "episode_rejected",
    }
    assert db_session.query(Episode).filter(Episode.episode_date == EPISODE_DATE).count() == 1


def test_run_ranking_selection_blocks_on_approved(db_session):
    episode = Episode(episode_date=EPISODE_DATE, status="approved")
    db_session.add(episode)
    db_session.commit()

    result = _run_ranking_selection(db_session, EPISODE_DATE, NOW)

    assert result["created"] is False
    assert result["reason"] == "episode_approved"


def test_run_ranking_selection_blocks_on_published(db_session):
    episode = Episode(episode_date=EPISODE_DATE, status="approved", publish_status="published")
    db_session.add(episode)
    db_session.commit()

    result = _run_ranking_selection(db_session, EPISODE_DATE, NOW)

    assert result["reason"] == "episode_published"


def test_unique_constraint_enforced_at_db_level(db_session):
    db_session.add(Episode(episode_date=EPISODE_DATE, status="draft"))
    db_session.commit()

    db_session.add(Episode(episode_date=EPISODE_DATE, status="draft"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_create_episode_or_recover_handles_lost_race(db_session):
    # A real committed Episode simulating another request that already
    # won the race for this episode_date.
    winner = Episode(episode_date=EPISODE_DATE, status="draft")
    db_session.add(winner)
    db_session.commit()

    # This call's own INSERT genuinely conflicts with the real
    # uq_editorial_episodes_episode_date constraint -- no mocking needed.
    episode_id, race_result = _create_episode_or_recover(db_session, EPISODE_DATE, top_slots=[])

    assert episode_id is None
    assert race_result["episode_id"] == winner.id
    assert race_result["created"] is False
    assert race_result["reason"] == "existing_draft_reused"
    assert race_result["race_lost"] is True
    # Session must still be usable afterward (rollback happened).
    assert db_session.query(Episode).filter(Episode.episode_date == EPISODE_DATE).count() == 1


# ---------------------------------------------------------
# _reprocess_episode
# ---------------------------------------------------------

def test_reprocess_episode_not_found(db_session):
    result = _reprocess_episode(db_session, 999999, NOW)
    assert result == {"episode_id": 999999, "reprocessed": False, "reason": "not_found"}


def test_reprocess_draft_replaces_snapshot_and_resets_state(db_session):
    story_a = _insert_story(db_session, "https://example.com/a")
    episode = Episode(episode_date=EPISODE_DATE, status="draft", video_status="ready", qa_status="passed")
    db_session.add(episode)
    db_session.flush()
    db_session.add(EpisodeStory(
        episode_id=episode.id, story_id=story_a.id, rank_position=1,
        selection_status="primary", rank_score=1.0, rank_reason="old",
    ))
    db_session.commit()

    story_b = _insert_story(db_session, "https://example.com/b")

    result = _reprocess_episode(db_session, episode.id, NOW)

    assert result["reprocessed"] is True
    assert result["episode_id"] == episode.id  # same episode, never a new one

    db_session.refresh(episode)
    assert episode.status == "draft"
    assert episode.video_status == "pending"
    assert episode.qa_status == "pending"
    assert episode.content_changed_at == NOW

    snapshot = (
        db_session.query(EpisodeStory).filter(EpisodeStory.episode_id == episode.id).all()
    )
    story_ids = {es.story_id for es in snapshot}
    # story_a is eligible again -- its own prior snapshot row (the
    # only thing that would have self-excluded it) was deleted before
    # scoring ran.
    assert story_ids == {story_a.id, story_b.id}
    assert db_session.query(Episode).filter(Episode.episode_date == EPISODE_DATE).count() == 1


def test_reprocess_rejected_allowed_and_resets_to_draft(db_session):
    episode = Episode(episode_date=EPISODE_DATE, status="rejected")
    db_session.add(episode)
    db_session.commit()
    _insert_story(db_session, "https://example.com/a")

    result = _reprocess_episode(db_session, episode.id, NOW)

    assert result["reprocessed"] is True
    db_session.refresh(episode)
    assert episode.status == "draft"


def test_reprocess_approved_blocked(db_session):
    episode = Episode(episode_date=EPISODE_DATE, status="approved")
    db_session.add(episode)
    db_session.commit()

    result = _reprocess_episode(db_session, episode.id, NOW)

    assert result == {
        "episode_id": episode.id,
        "episode_date": EPISODE_DATE.isoformat(),
        "reprocessed": False,
        "reason": "episode_approved",
    }


def test_reprocess_published_blocked(db_session):
    episode = Episode(episode_date=EPISODE_DATE, status="approved", publish_status="published")
    db_session.add(episode)
    db_session.commit()

    result = _reprocess_episode(db_session, episode.id, NOW)

    assert result["reprocessed"] is False
    assert result["reason"] == "episode_published"


def test_reprocess_does_not_create_second_episode(db_session):
    episode = Episode(episode_date=EPISODE_DATE, status="draft")
    db_session.add(episode)
    db_session.commit()
    _insert_story(db_session, "https://example.com/a")

    _reprocess_episode(db_session, episode.id, NOW)

    assert db_session.query(Episode).filter(Episode.episode_date == EPISODE_DATE).count() == 1


# ---------------------------------------------------------
# API endpoints -- POST /api/v1/episodes/select and
# POST /api/v1/episodes/{episode_id}/reprocess. No httpx/TestClient in
# this project's dependencies, so these call the FastAPI route
# functions directly (same pattern tests/test_ingestion_endpoints.py
# already uses). Unlike a plain ingestion trigger, these now do a real
# synchronous DB read before deciding whether to queue anything, so
# app.main.SessionLocal is monkeypatched to a dedicated in-memory
# SQLite engine (StaticPool, so every session opened against it -- the
# endpoint's own and this test's setup session -- shares one
# connection and therefore one consistent view of the data).
# ---------------------------------------------------------

@pytest.fixture
def sqlite_session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Same schema_translate_map as tests/conftest.py's db_session
    # fixture -- SQLite has no real multi-schema support, so the real
    # raw/editorial Postgres schemas are mapped away for this engine
    # only. See that fixture's docstring for the full rationale.
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


def _json_body(response: JSONResponse) -> dict:
    return json.loads(response.body)


def test_select_endpoint_queues_when_no_existing_episode(monkeypatch, sqlite_session_factory):
    monkeypatch.setattr(main, "SessionLocal", sqlite_session_factory)
    mock_delay = MagicMock(return_value=MagicMock(id="fake-task-id"))
    monkeypatch.setattr(run_daily_processing, "delay", mock_delay)

    result = main.trigger_ranking_selection(episode_date="2026-09-22")

    assert result == {"task_id": "fake-task-id", "status": "queued"}
    mock_delay.assert_called_once_with("2026-09-22")


def test_select_endpoint_reuses_existing_draft_without_queuing(monkeypatch, sqlite_session_factory, api_db):
    monkeypatch.setattr(main, "SessionLocal", sqlite_session_factory)
    episode = Episode(episode_date=EPISODE_DATE, status="draft")
    api_db.add(episode)
    api_db.commit()
    mock_delay = MagicMock()
    monkeypatch.setattr(run_daily_processing, "delay", mock_delay)

    result = main.trigger_ranking_selection(episode_date="2026-09-22")

    assert result == {
        "episode_id": episode.id,
        "episode_date": "2026-09-22",
        "created": False,
        "reason": "existing_draft_reused",
    }
    mock_delay.assert_not_called()


def test_select_endpoint_rejected_returns_409(monkeypatch, sqlite_session_factory, api_db):
    monkeypatch.setattr(main, "SessionLocal", sqlite_session_factory)
    episode = Episode(episode_date=EPISODE_DATE, status="rejected")
    api_db.add(episode)
    api_db.commit()
    mock_delay = MagicMock()
    monkeypatch.setattr(run_daily_processing, "delay", mock_delay)

    response = main.trigger_ranking_selection(episode_date="2026-09-22")

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    body = _json_body(response)
    assert body["error"] == "episode_rejected"
    assert body["episode_id"] == episode.id
    mock_delay.assert_not_called()


def test_select_endpoint_approved_returns_409(monkeypatch, sqlite_session_factory, api_db):
    monkeypatch.setattr(main, "SessionLocal", sqlite_session_factory)
    episode = Episode(episode_date=EPISODE_DATE, status="approved")
    api_db.add(episode)
    api_db.commit()
    mock_delay = MagicMock()
    monkeypatch.setattr(run_daily_processing, "delay", mock_delay)

    response = main.trigger_ranking_selection(episode_date="2026-09-22")

    assert response.status_code == 409
    assert _json_body(response)["error"] == "episode_approved"
    mock_delay.assert_not_called()


def test_select_endpoint_published_returns_409(monkeypatch, sqlite_session_factory, api_db):
    monkeypatch.setattr(main, "SessionLocal", sqlite_session_factory)
    episode = Episode(episode_date=EPISODE_DATE, status="approved", publish_status="published")
    api_db.add(episode)
    api_db.commit()
    mock_delay = MagicMock()
    monkeypatch.setattr(run_daily_processing, "delay", mock_delay)

    response = main.trigger_ranking_selection(episode_date="2026-09-22")

    assert response.status_code == 409
    assert _json_body(response)["error"] == "episode_published"
    mock_delay.assert_not_called()


# "Multiple existing episodes" is not exercised at the endpoint level
# for the same reason noted on _classify_episode_lookup above:
# uq_editorial_episodes_episode_date (present in this fixture's
# metadata too) makes it impossible to insert two rows for one
# episode_date, even in a test. Covered directly via
# _classify_episode_lookup's own unit tests instead.


def test_reprocess_endpoint_not_found(monkeypatch, sqlite_session_factory):
    monkeypatch.setattr(main, "SessionLocal", sqlite_session_factory)

    with pytest.raises(HTTPException) as exc_info:
        main.reprocess_episode_endpoint(999999)

    assert exc_info.value.status_code == 404


def test_reprocess_endpoint_queues_for_draft(monkeypatch, sqlite_session_factory, api_db):
    monkeypatch.setattr(main, "SessionLocal", sqlite_session_factory)
    episode = Episode(episode_date=EPISODE_DATE, status="draft")
    api_db.add(episode)
    api_db.commit()
    mock_delay = MagicMock(return_value=MagicMock(id="fake-task-id"))
    monkeypatch.setattr(reprocess_episode, "delay", mock_delay)

    result = main.reprocess_episode_endpoint(episode.id)

    assert result == {"episode_id": episode.id, "task_id": "fake-task-id", "status": "queued"}
    mock_delay.assert_called_once_with(episode.id)


def test_reprocess_endpoint_queues_for_rejected(monkeypatch, sqlite_session_factory, api_db):
    monkeypatch.setattr(main, "SessionLocal", sqlite_session_factory)
    episode = Episode(episode_date=EPISODE_DATE, status="rejected")
    api_db.add(episode)
    api_db.commit()
    mock_delay = MagicMock(return_value=MagicMock(id="fake-task-id"))
    monkeypatch.setattr(reprocess_episode, "delay", mock_delay)

    result = main.reprocess_episode_endpoint(episode.id)

    assert result["status"] == "queued"
    mock_delay.assert_called_once_with(episode.id)


def test_reprocess_endpoint_blocks_approved(monkeypatch, sqlite_session_factory, api_db):
    monkeypatch.setattr(main, "SessionLocal", sqlite_session_factory)
    episode = Episode(episode_date=EPISODE_DATE, status="approved")
    api_db.add(episode)
    api_db.commit()
    mock_delay = MagicMock()
    monkeypatch.setattr(reprocess_episode, "delay", mock_delay)

    response = main.reprocess_episode_endpoint(episode.id)

    assert response.status_code == 409
    assert _json_body(response)["error"] == "episode_approved"
    mock_delay.assert_not_called()


def test_reprocess_endpoint_blocks_published(monkeypatch, sqlite_session_factory, api_db):
    monkeypatch.setattr(main, "SessionLocal", sqlite_session_factory)
    episode = Episode(episode_date=EPISODE_DATE, status="approved", publish_status="published")
    api_db.add(episode)
    api_db.commit()
    mock_delay = MagicMock()
    monkeypatch.setattr(reprocess_episode, "delay", mock_delay)

    response = main.reprocess_episode_endpoint(episode.id)

    assert response.status_code == 409
    assert _json_body(response)["error"] == "episode_published"
    mock_delay.assert_not_called()
