"""
Stage buttons re-run against a draft episode must never disturb the
first run's stories (title dedup, content dedup) -- "first run sticks
until approval".
"""
from datetime import date, datetime, timedelta

from app.content.article_extractor import ArticleExtractionResult
from app.models import Episode, EpisodeStory, NewsItem, StoryState
from app.tasks import content_dedup
from app.tasks.dedup import deduplicate_new_stories, pinned_story_ids


TARGET = date(2026, 9, 22)
BASE = datetime(2026, 9, 22, 8, 0)


def _story(db, title, url, published_at, summary=None):
    item = NewsItem(
        title=title,
        canonical_url=url,
        source_name="Example Source",
        source_type="rss",
        published_at=published_at,
        collected_at=BASE,
        collection_date=TARGET,
        raw_summary=summary,
        status="collected",
    )
    db.add(item)
    db.flush()
    db.add(StoryState(id=item.id, ai_relevance="ai_candidate", ai_relevance_score=0.8))
    db.commit()
    return item


def _pin(db, item, position=1, status="primary"):
    episode = db.query(Episode).filter(Episode.episode_date == TARGET).first()
    if episode is None:
        episode = Episode(episode_date=TARGET, status="draft")
        db.add(episode)
        db.flush()
    db.add(EpisodeStory(
        episode_id=episode.id, story_id=item.id,
        rank_position=position, selection_status=status, rank_score=1.0, rank_reason="test",
    ))
    db.commit()
    return episode


def test_pinned_story_ids_covers_primary_and_backup(db_session):
    a = _story(db_session, "A", "https://e.test/a", BASE)
    b = _story(db_session, "B", "https://e.test/b", BASE)
    _story(db_session, "C", "https://e.test/c", BASE)
    _pin(db_session, a, 1, "primary")
    _pin(db_session, b, 26, "backup")

    assert pinned_story_ids(db_session, TARGET) == {a.id, b.id}
    assert pinned_story_ids(db_session, date(2026, 9, 23)) == set()


def test_title_dedup_keeps_pinned_story_canonical_over_older_newcomer(db_session):
    title = "OpenAI releases GPT-6 with a million token context window"
    newer_pinned = _story(db_session, title, "https://e.test/pinned", BASE + timedelta(hours=2))
    older_new = _story(db_session, title, "https://e.test/older", BASE)
    _pin(db_session, newer_pinned)

    result = deduplicate_new_stories(db_session, TARGET)

    assert result["duplicates_found"] == 1
    pinned_state = db_session.get(StoryState, newer_pinned.id)
    older_state = db_session.get(StoryState, older_new.id)
    assert pinned_state.canonical_story_id is None
    assert older_state.canonical_story_id == newer_pinned.id


def test_title_dedup_without_pin_still_prefers_oldest(db_session):
    title = "OpenAI releases GPT-6 with a million token context window"
    newer = _story(db_session, title, "https://e.test/newer", BASE + timedelta(hours=2))
    older = _story(db_session, title, "https://e.test/older", BASE)

    deduplicate_new_stories(db_session, TARGET)

    assert db_session.get(StoryState, older.id).canonical_story_id is None
    assert db_session.get(StoryState, newer.id).canonical_story_id == older.id


def test_content_dedup_never_demotes_pinned_backup(db_session, monkeypatch):
    monkeypatch.setattr(
        content_dedup, "fetch_full_article_text",
        lambda url: ArticleExtractionResult(text=None, status="fetch_error"),
    )
    monkeypatch.setattr(content_dedup.time, "sleep", lambda seconds: None)

    summary = (
        "Anthropic announced a new model family with stronger reasoning, longer "
        "context and lower pricing for enterprise customers building agents."
    )
    pinned_backup = _story(
        db_session, "Anthropic unveils new model family", "https://e.test/backup",
        BASE + timedelta(hours=3), summary,
    )
    older_new = _story(
        db_session, "Anthropic launches stronger reasoning models", "https://e.test/older",
        BASE, summary,
    )
    # A third, unrelated story keeps shared terms under TF-IDF's max_df cut-off.
    _story(
        db_session, "Robotics lab unveils warehouse hardware", "https://e.test/other",
        BASE + timedelta(hours=1), "Boston startup shows a humanoid picking parcels in logistics trials.",
    )
    _pin(db_session, pinned_backup, 26, "backup")

    content_dedup.enrich_and_dedup_by_content(db_session, TARGET)

    assert db_session.get(StoryState, pinned_backup.id).canonical_story_id is None
    assert db_session.get(StoryState, older_new.id).canonical_story_id == pinned_backup.id
