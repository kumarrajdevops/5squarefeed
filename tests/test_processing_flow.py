from datetime import date, datetime, timedelta, timezone

from app.content.article_extractor import ArticleExtractionResult
from app.dates import coverage_window
from app.models import Episode, EpisodeStory, NewsItem, StoryState
from app.tasks import content_dedup, scheduled


TARGET_DATE = date(2026, 9, 20)


def _insert_raw_item(db, title, url, source_name="Test Source"):
    coverage_start, _ = coverage_window(TARGET_DATE)
    published_at = coverage_start + timedelta(hours=1)

    item = NewsItem(
        title=title,
        canonical_url=url,
        source_name=source_name,
        source_type="rss",
        published_at=published_at,
        collected_at=datetime.now(timezone.utc),
        collection_date=TARGET_DATE,
        raw_summary=f"{title} -- a real update from {source_name}.",
        status="collected",
    )
    db.add(item)
    db.commit()
    return item


class _FixedNow(datetime):
    """
    SQLite (this fixture's backend) drops tzinfo on round-trip through
    a DateTime(timezone=True) column, so a NewsItem's published_at read
    back via db_session is naive regardless of what was written.
    run_daily_processing computes its own `now = datetime.now(timezone.
    utc)` (aware) internally, which would otherwise raise
    TypeError when compute_recency_score subtracts the two. Same root
    cause/fix as tests/test_episode_idempotency.py's NOW constant --
    here `now` isn't a caller-supplied argument, so it's patched at
    the datetime.now() call site instead. Real production code is
    unaffected -- it always runs against Postgres, which preserves
    tzinfo faithfully.
    """
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 21, 12, 0)


def _stub_out_production(monkeypatch):
    # produce_episode_video/run_episode_qa are the full media pipeline
    # (ffmpeg, TTS, etc) -- entirely out of scope for a test of the
    # raw -> editorial dataflow, so stubbed out here rather than run
    # for real.
    monkeypatch.setattr(scheduled, "produce_episode_video", lambda episode_id: {"status": "ready"})
    monkeypatch.setattr(scheduled, "run_episode_qa", lambda episode_id: {"qa_status": "passed"})

    # content_dedup's full-article fetch is a real network call --
    # never exercised here; every story degrades to raw_summary/title
    # comparison, same as this stage's real never-fails contract.
    monkeypatch.setattr(
        content_dedup, "fetch_full_article_text",
        lambda url: ArticleExtractionResult(text=None, status="fetch_error"),
    )
    monkeypatch.setattr(content_dedup.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(scheduled, "datetime", _FixedNow)


def test_run_daily_processing_creates_editorial_state_and_one_episode(db_session, monkeypatch):
    _stub_out_production(monkeypatch)
    _insert_raw_item(db_session, "OpenAI announces new AI model", "https://example.test/a")
    _insert_raw_item(db_session, "Robotics lab unveils warehouse automation hardware", "https://example.test/b")

    # SessionLocal is normally a context manager (`with SessionLocal() as db`);
    # db_session itself supports the same protocol via SQLAlchemy's Session.
    monkeypatch.setattr(scheduled, "SessionLocal", lambda: db_session)
    result = scheduled.run_daily_processing(TARGET_DATE.isoformat())

    assert result["classify"]["classified"] == 2
    assert result["classify"]["ai_candidates"] == 2
    assert result["ranking"]["created"] is True

    episodes = db_session.query(Episode).filter(Episode.episode_date == TARGET_DATE).all()
    assert len(episodes) == 1

    states = db_session.query(StoryState).all()
    assert len(states) == 2
    assert all(s.ai_relevance == "ai_candidate" for s in states)

    selected = (
        db_session.query(EpisodeStory)
        .filter(EpisodeStory.episode_id == episodes[0].id)
        .all()
    )
    assert len(selected) == 2

    assert result["produce_status"] == "ready"
    assert result["qa_status"] == "passed"


def test_run_daily_processing_is_idempotent_for_the_same_target_date(db_session, monkeypatch):
    _stub_out_production(monkeypatch)
    _insert_raw_item(db_session, "OpenAI announces new AI model", "https://example.test/a")

    monkeypatch.setattr(scheduled, "SessionLocal", lambda: db_session)
    first = scheduled.run_daily_processing(TARGET_DATE.isoformat())
    # The stubbed produce doesn't touch the episode; mark it produced the
    # way the real task would.
    db_session.query(Episode).filter(Episode.episode_date == TARGET_DATE).one().video_status = "ready"
    db_session.commit()
    second = scheduled.run_daily_processing(TARGET_DATE.isoformat())

    assert first["ranking"]["created"] is True
    assert second["ranking"]["created"] is False
    assert second["ranking"]["updated"] is True
    assert second["ranking"]["reason"] == "existing_draft_updated"
    assert second["ranking"]["added_primary"] == 0

    # Re-running never creates a second episode for the same date, and
    # never re-classifies an already-classified raw item.
    assert db_session.query(Episode).filter(Episode.episode_date == TARGET_DATE).count() == 1
    assert second["classify"]["classified"] == 0

    # Nothing new and the video is already ready -> no re-produce.
    assert second["produce_status"] == "unchanged"


def test_run_daily_processing_update_keeps_first_run_and_reproduces(db_session, monkeypatch):
    _stub_out_production(monkeypatch)
    first_id = _insert_raw_item(db_session, "OpenAI announces new AI model", "https://example.test/a").id

    monkeypatch.setattr(scheduled, "SessionLocal", lambda: db_session)
    first = scheduled.run_daily_processing(TARGET_DATE.isoformat())
    episode_id = first["ranking"]["episode_id"]
    episode = db_session.get(Episode, episode_id)
    episode.video_status = "ready"
    db_session.commit()

    new_id = _insert_raw_item(db_session, "Robotics lab unveils warehouse automation hardware", "https://example.test/b").id
    # SQLite drops tzinfo on reload; don't keep the aware instance around.
    db_session.expunge_all()
    second = scheduled.run_daily_processing(TARGET_DATE.isoformat())

    assert second["ranking"]["updated"] is True
    assert second["ranking"]["added_primary"] == 1
    assert second["produce_status"] == "ready"

    rows = {
        r.story_id: r.rank_position
        for r in db_session.query(EpisodeStory).filter(EpisodeStory.episode_id == episode_id).all()
    }
    assert rows[first_id] == 1
    assert new_id in rows


def test_run_daily_processing_does_not_produce_for_approved_episode(db_session, monkeypatch):
    produced = []
    _stub_out_production(monkeypatch)
    monkeypatch.setattr(
        scheduled, "produce_episode_video",
        lambda episode_id: produced.append(episode_id) or {"status": "ready"},
    )
    db_session.add(Episode(episode_date=TARGET_DATE, status="approved"))
    db_session.commit()

    monkeypatch.setattr(scheduled, "SessionLocal", lambda: db_session)
    result = scheduled.run_daily_processing(TARGET_DATE.isoformat())

    assert result["ranking"]["reason"] == "episode_approved"
    assert produced == []
