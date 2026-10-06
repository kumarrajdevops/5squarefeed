"""
Focused tests for app.main._compute_pipeline_stages -- the dashboard's
visual workflow chart data source (see TODO.md). Purely derived from
existing data (CollectionRun, StoryState, the episode's own fields); no
new persistence. Collect -> Rank & Select are, by construction, already
"done" for any episode that exists at all (rank/select is what creates
the row) -- these tests confirm the real COUNTS shown are correct, and
that Produce/QA/Approve/Publish correctly reflect the episode's own
live status fields.
"""
from datetime import date, datetime, timezone

from app.main import _compute_pipeline_stages
from app.dedup.decision import METHOD_VERSION
from app.models import CollectionRun, Episode, HistoricalStoryRelation, NewsItem, StoryState


EPISODE_DATE = date(2026, 9, 29)


def _make_episode(db, **overrides):
    defaults = dict(episode_date=EPISODE_DATE, status="draft")
    defaults.update(overrides)
    episode = Episode(**defaults)
    db.add(episode)
    db.flush()
    return episode


def _make_news_item(db, slug, **overrides):
    defaults = dict(
        title=f"Story {slug}",
        canonical_url=f"https://example.com/{slug}",
        source_name="Example Source",
        source_type="rss",
        published_at=datetime.now(timezone.utc),
        collected_at=datetime.now(timezone.utc),
        collection_date=EPISODE_DATE,
        status="collected",
    )
    defaults.update(overrides)
    item = NewsItem(**defaults)
    db.add(item)
    db.flush()
    return item


def _stage(stages, name):
    return next(s for s in stages if s["stage"] == name)


def test_nothing_run_yet_shows_pending_stages_except_rank_select(db_session):
    episode = _make_episode(db_session)
    db_session.commit()

    stages = _compute_pipeline_stages(db_session, episode, primary_count=0, backup_count=0)

    assert _stage(stages, "collect")["status"] == "pending"
    assert _stage(stages, "classify")["status"] == "pending"
    assert _stage(stages, "dedup")["status"] == "pending"
    assert _stage(stages, "content_dedup")["status"] == "pending"
    assert _stage(stages, "verify")["status"] == "pending"
    # Trivially done: the episode existing at all means rank/select
    # already ran (it's what created this row).
    assert _stage(stages, "rank_select")["status"] == "done"
    assert _stage(stages, "produce")["status"] == "pending"
    assert _stage(stages, "qa")["status"] == "pending"
    assert _stage(stages, "approve")["status"] == "pending"
    assert _stage(stages, "publish")["status"] == "pending"


def test_collection_run_success_shows_real_counts(db_session):
    episode = _make_episode(db_session)
    db_session.add(CollectionRun(
        collection_date=EPISODE_DATE, status="success",
        rss_items_seen=40, rss_items_inserted=12, rss_items_updated=3,
        hn_items_seen=20, hn_items_inserted=5, hn_items_updated=1,
    ))
    db_session.commit()

    stages = _compute_pipeline_stages(db_session, episode, primary_count=0, backup_count=0)

    collect = _stage(stages, "collect")
    assert collect["status"] == "done"
    assert collect["detail"] == "60 seen, 17 new"


def test_collection_run_failure_is_shown_as_failed(db_session):
    episode = _make_episode(db_session)
    db_session.add(CollectionRun(collection_date=EPISODE_DATE, status="failed", error_count=3))
    db_session.commit()

    stages = _compute_pipeline_stages(db_session, episode, primary_count=0, backup_count=0)

    collect = _stage(stages, "collect")
    assert collect["status"] == "failed"
    assert collect["detail"] == "3 error(s)"


def test_processed_stories_show_real_counts_across_all_four_stages(db_session):
    episode = _make_episode(db_session)
    a = _make_news_item(db_session, "a")
    b = _make_news_item(db_session, "b")
    c = _make_news_item(db_session, "c")
    db_session.add(StoryState(id=a.id, ai_relevance="ai_candidate", verification_status="verified"))
    db_session.add(StoryState(
        id=b.id, ai_relevance="not_relevant", verification_status="unverified",
        canonical_story_id=a.id, content_fetch_status="success", repeats_story_id=a.id,
    ))
    db_session.add(StoryState(id=c.id, ai_relevance="ai_candidate", verification_status="pending"))
    db_session.commit()

    stages = _compute_pipeline_stages(db_session, episode, primary_count=1, backup_count=0)

    assert _stage(stages, "classify")["status"] == "done"
    assert _stage(stages, "classify")["detail"] == "3 classified, 2 AI candidates"
    assert _stage(stages, "dedup")["detail"] == "1 duplicate(s) removed"
    assert _stage(stages, "content_dedup")["detail"] == "1 article(s) fetched"
    assert _stage(stages, "historical_dedup")["status"] == "pending"
    assert _stage(stages, "verify")["detail"] == "1 verified, 1 unverified"
    assert _stage(stages, "rank_select")["detail"] == "1 primary, 0 backup selected"


def test_historical_dedup_stage_counts_repeats_and_new_developments(db_session):
    episode = _make_episode(db_session)
    old = _make_news_item(db_session, "old")
    a = _make_news_item(db_session, "a")
    b = _make_news_item(db_session, "b")
    for item in (old, a, b):
        db_session.add(StoryState(id=item.id, ai_relevance="ai_candidate", verification_status="pending"))
    db_session.flush()
    for story, decision in ((a, "duplicate"), (b, "new_development")):
        db_session.add(HistoricalStoryRelation(
            story_id=story.id, matched_story_id=old.id, decision=decision, semantic_similarity=0.9,
            development_match="same", rule="r", reason="x", content_basis="full", method_version=METHOD_VERSION,
        ))
    db_session.commit()

    stages = _compute_pipeline_stages(db_session, episode, primary_count=0, backup_count=0)

    stage = _stage(stages, "historical_dedup")
    assert stage["status"] == "done"
    assert stage["detail"].startswith("1 repeat(s) held back, 1 new development(s)")


def test_produce_status_reflects_video_status(db_session):
    cases = [("pending", "pending"), ("producing", "pending"), ("ready", "done"), ("failed", "failed")]
    for i, (video_status, expected) in enumerate(cases):
        episode = _make_episode(db_session, episode_date=date(2026, 9, 20 + i), video_status=video_status)
        db_session.commit()
        stages = _compute_pipeline_stages(db_session, episode, primary_count=0, backup_count=0)
        assert _stage(stages, "produce")["status"] == expected, video_status


def test_qa_failure_is_a_warning_not_a_hard_failure(db_session):
    import json
    episode = _make_episode(
        db_session,
        qa_status="failed",
        qa_report=json.dumps([
            {"check": "story_count", "passed": True},
            {"check": "duration_target", "passed": False},
            {"check": "source_verification", "passed": False},
        ]),
    )
    db_session.commit()

    stages = _compute_pipeline_stages(db_session, episode, primary_count=0, backup_count=0)

    qa = _stage(stages, "qa")
    assert qa["status"] == "warn"
    assert qa["detail"] == "2 check(s) failed"


def test_qa_pass_is_done(db_session):
    episode = _make_episode(db_session, qa_status="passed")
    db_session.commit()

    stages = _compute_pipeline_stages(db_session, episode, primary_count=0, backup_count=0)

    assert _stage(stages, "qa")["status"] == "done"


def test_approve_and_publish_status_reflect_episode_fields(db_session):
    episode = _make_episode(db_session, status="approved", publish_status="published")
    db_session.commit()

    stages = _compute_pipeline_stages(db_session, episode, primary_count=0, backup_count=0)

    assert _stage(stages, "approve")["status"] == "done"
    assert _stage(stages, "publish")["status"] == "done"


def test_rejected_and_publish_failed_are_hard_failures(db_session):
    episode = _make_episode(
        db_session, status="rejected", publish_status="failed", publish_error="quota exceeded",
    )
    db_session.commit()

    stages = _compute_pipeline_stages(db_session, episode, primary_count=0, backup_count=0)

    assert _stage(stages, "approve")["status"] == "failed"
    publish = _stage(stages, "publish")
    assert publish["status"] == "failed"
    assert publish["detail"] == "quota exceeded"


def test_classify_detail_reports_stories_awaiting_review(db_session):
    episode = _make_episode(db_session)
    a = _make_news_item(db_session, "a")
    b = _make_news_item(db_session, "b")
    c = _make_news_item(db_session, "c")
    db_session.add(StoryState(id=a.id, ai_relevance="ai_candidate"))
    db_session.add(StoryState(id=b.id, ai_relevance="ai_review"))
    db_session.add(StoryState(id=c.id, ai_relevance="not_ai"))
    db_session.commit()

    stages = _compute_pipeline_stages(db_session, episode, primary_count=0, backup_count=0)

    assert _stage(stages, "classify")["detail"] == "3 classified, 1 AI candidates, 1 to review"
