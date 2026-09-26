"""
Focused tests for app.content.episode_renderer's episode/story lookup and
storyboard-prerequisite orchestration -- the parts of the production-
pipeline consolidation that don't require actually running ffmpeg/Pillow.
Confirms: no hardcoded story IDs/episode ID/date, DB-stored rank order is
used unmodified, and a missing episode/empty selection raises clearly.
"""
from datetime import date, datetime, timezone

import pytest

from app.content import episode_renderer
from app.models import Episode, EpisodeStory, NewsItem, StoryState


def _make_story(db, **overrides):
    defaults = dict(
        title="A Story",
        canonical_url=f"https://example.com/{overrides.get('_slug', 'story')}",
        source_name="Example Source",
        source_type="rss",
        published_at=datetime.now(timezone.utc),
        collected_at=datetime.now(timezone.utc),
        collection_date=date(2026, 9, 22),
        status="collected",
        raw_summary="Some raw summary.",
    )
    overrides.pop("_slug", None)
    defaults.update(overrides)
    story = NewsItem(**defaults)
    db.add(story)
    db.flush()
    db.add(StoryState(id=story.id))
    db.flush()
    return story


def test_load_episode_stories_raises_for_missing_episode(db_session, monkeypatch):
    monkeypatch.setattr(episode_renderer, "SessionLocal", lambda: db_session)
    db_session.close = lambda: None  # the context-manager `with` calls __exit__ -> close(); keep it open for the test
    with pytest.raises(ValueError, match="not found"):
        episode_renderer.load_episode_stories(999999)


def test_load_episode_stories_raises_when_no_primary_stories(db_session, monkeypatch):
    monkeypatch.setattr(episode_renderer, "SessionLocal", lambda: db_session)
    db_session.close = lambda: None
    episode = Episode(episode_date=date(2026, 9, 22), status="draft")
    db_session.add(episode)
    db_session.flush()

    with pytest.raises(ValueError, match="no primary stories"):
        episode_renderer.load_episode_stories(episode.id)


def test_load_episode_stories_returns_db_rank_order_unmodified(db_session, monkeypatch):
    """The exact ordering rule this task requires: rank_position from the
    DB, ascending, never re-sorted/re-ranked by this function -- even
    when inserted out of order and even with a non-contiguous, descending
    rank_position sequence."""
    monkeypatch.setattr(episode_renderer, "SessionLocal", lambda: db_session)
    db_session.close = lambda: None

    episode = Episode(episode_date=date(2026, 9, 23), status="draft")
    db_session.add(episode)
    db_session.flush()

    stories = [_make_story(db_session, _slug=f"s{i}") for i in range(3)]
    # Insert with rank_position deliberately out of story-creation order.
    for story, rank in zip(stories, [30, 10, 20]):
        db_session.add(EpisodeStory(
            episode_id=episode.id, story_id=story.id,
            rank_position=rank, selection_status="primary", rank_score=1.0,
        ))
    db_session.flush()

    story_ids, episode_date = episode_renderer.load_episode_stories(episode.id)

    # rank 10 -> stories[1], rank 20 -> stories[2], rank 30 -> stories[0]
    assert story_ids == [stories[1].id, stories[2].id, stories[0].id]
    assert episode_date == "September 23, 2026"


def test_load_episode_stories_ignores_backup_stories(db_session, monkeypatch):
    monkeypatch.setattr(episode_renderer, "SessionLocal", lambda: db_session)
    db_session.close = lambda: None

    episode = Episode(episode_date=date(2026, 9, 24), status="draft")
    db_session.add(episode)
    db_session.flush()

    primary = _make_story(db_session, _slug="primary")
    backup = _make_story(db_session, _slug="backup")
    db_session.add(EpisodeStory(
        episode_id=episode.id, story_id=primary.id,
        rank_position=1, selection_status="primary", rank_score=1.0,
    ))
    db_session.add(EpisodeStory(
        episode_id=episode.id, story_id=backup.id,
        rank_position=2, selection_status="backup", rank_score=0.5,
    ))
    db_session.flush()

    story_ids, _ = episode_renderer.load_episode_stories(episode.id)

    assert story_ids == [primary.id]


def test_ensure_all_storyboards_stops_on_genuine_failure_before_regenerating_further(monkeypatch):
    """Rule 11/12: a genuine failure for one story must stop the whole
    prerequisite pass with a clear error, not silently continue and
    produce an incomplete episode."""
    calls = []

    def fake_ensure_storyboard(db, story_id):
        calls.append(story_id)
        if story_id == 2:
            return {"story_id": story_id, "status": "failed", "stage": "storyboard_compose", "error": "boom"}
        return {"story_id": story_id, "status": "ready", "reused": True}

    monkeypatch.setattr(episode_renderer, "ensure_storyboard", fake_ensure_storyboard)
    monkeypatch.setattr(episode_renderer, "SessionLocal", lambda: _FakeSessionCtx())

    with pytest.raises(RuntimeError, match="story 2"):
        episode_renderer.ensure_all_storyboards([1, 2, 3])


def test_ensure_all_storyboards_treats_qa_failed_as_usable_not_fatal(monkeypatch):
    def fake_ensure_storyboard(db, story_id):
        return {"story_id": story_id, "status": "qa_failed", "reused": False}

    monkeypatch.setattr(episode_renderer, "ensure_storyboard", fake_ensure_storyboard)
    monkeypatch.setattr(episode_renderer, "SessionLocal", lambda: _FakeSessionCtx())

    result = episode_renderer.ensure_all_storyboards([1, 2])
    assert result["regenerated"] == [1, 2]
    assert result["reused"] == []


class _FakeSessionCtx:
    """Minimal stand-in for `with SessionLocal() as db:` when the body
    only ever passes `db` through to a monkeypatched function that
    ignores it."""
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
