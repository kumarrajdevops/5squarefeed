"""
The human-editor review lane: ai_review stories stay out of every pool until an
editor promotes them (-> ai_candidate) or rejects them (-> not_ai).
"""
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from app import main
from app.filters.review import (
    ReviewError,
    apply_review_decision,
)
from app.models import Episode, EpisodeStory, NewsItem, StoryState
from app.tasks import scheduled
from tests.test_processing_flow import (
    TARGET_DATE,
    _insert_raw_item,
    _stub_out_production,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
REVIEW_TITLE = "The future of AI agents in banking"
CANDIDATE_TITLE = "OpenAI announces new AI model"


def _review_story(db, title="A review story", score=0.2, state="ai_review"):
    item = NewsItem(
        title=title,
        canonical_url=f"https://e.test/{abs(hash(title))}",
        source_name="Test Source",
        source_type="rss",
        published_at=datetime(2026, 9, 20, 8, 0),
        collected_at=datetime(2026, 9, 20, 9, 0),
        collection_date=date(2026, 9, 20),
        raw_summary="Summary text.",
        status="collected",
    )
    db.add(item)
    db.flush()
    db.add(
        StoryState(
            id=item.id,
            ai_relevance=state,
            ai_relevance_score=score,
            filter_reason="Review: AI term in title but no development verb",
            classifier_version="rules-v1",
            classifier_disposition="review",
            classifier_ai_relatedness="core",
        )
    )
    db.commit()
    return item


# --- apply_review_decision ------------------------------------------------------


def test_promote_makes_the_story_a_candidate_and_records_the_decision(db_session):
    item = _review_story(db_session, score=0.0)

    state = apply_review_decision(db_session, item.id, "promote", now=NOW)

    assert state.ai_relevance == "ai_candidate"
    assert state.review_decision == "promoted"
    assert state.reviewed_at == NOW


@pytest.mark.parametrize("score", [0.0, 0.4, 0.9])
def test_promote_never_changes_the_score(db_session, score):
    item = _review_story(db_session, score=score)
    assert apply_review_decision(db_session, item.id, "promote").ai_relevance_score == score


def test_reject_makes_the_story_not_ai_and_records_the_decision(db_session):
    item = _review_story(db_session, score=0.4)

    state = apply_review_decision(db_session, item.id, "reject", now=NOW)

    assert state.ai_relevance == "not_ai"
    assert state.review_decision == "rejected"
    assert state.reviewed_at == NOW
    assert state.ai_relevance_score == 0.4


def test_decision_keeps_the_original_classifier_record(db_session):
    item = _review_story(db_session)
    state = apply_review_decision(db_session, item.id, "promote")

    # The editor override sits beside the classifier's own explanation, not over it.
    assert state.classifier_version == "rules-v1"
    assert state.classifier_disposition == "review"
    assert state.filter_reason.startswith("Review:")


def test_decision_is_persisted(db_session):
    item = _review_story(db_session)
    apply_review_decision(db_session, item.id, "promote")
    db_session.expire_all()
    assert db_session.get(StoryState, item.id).ai_relevance == "ai_candidate"


@pytest.mark.parametrize("state", ["ai_candidate", "not_ai"])
def test_only_review_stories_can_be_decided(db_session, state):
    item = _review_story(db_session, state=state)

    for decision in ("promote", "reject"):
        with pytest.raises(ReviewError) as err:
            apply_review_decision(db_session, item.id, decision)
        assert err.value.status_code == 409

    assert db_session.get(StoryState, item.id).ai_relevance == state


def test_a_decision_cannot_be_applied_twice(db_session):
    item = _review_story(db_session)
    apply_review_decision(db_session, item.id, "reject")

    with pytest.raises(ReviewError) as err:
        apply_review_decision(db_session, item.id, "promote")
    assert err.value.status_code == 409
    assert db_session.get(StoryState, item.id).ai_relevance == "not_ai"


def test_unknown_story_is_404(db_session):
    with pytest.raises(ReviewError) as err:
        apply_review_decision(db_session, 99999, "promote")
    assert err.value.status_code == 404


def test_unknown_decision_is_422_and_changes_nothing(db_session):
    item = _review_story(db_session)

    with pytest.raises(ReviewError) as err:
        apply_review_decision(db_session, item.id, "maybe")

    assert err.value.status_code == 422
    assert db_session.get(StoryState, item.id).ai_relevance == "ai_review"


# --- review stories stay out of ranking until promoted --------------------------


def _episode_story_ids(db):
    episode = db.query(Episode).filter(Episode.episode_date == TARGET_DATE).one()
    return {
        row.story_id
        for row in db.query(EpisodeStory).filter(EpisodeStory.episode_id == episode.id).all()
    }


def _raw_id(db_session, title, url):
    # Only the id is kept: a held NewsItem would keep its tz-aware published_at in the session
    # cache, whereas SQLite hands it back naive (see _FixedNow in test_processing_flow).
    return _insert_raw_item(db_session, title, url).id


def _process(db_session, monkeypatch):
    monkeypatch.setattr(scheduled, "SessionLocal", lambda: db_session)
    return scheduled.run_daily_processing(TARGET_DATE.isoformat())


def _mark_episode_ready(db_session):
    db_session.query(Episode).filter(Episode.episode_date == TARGET_DATE).one().video_status = "ready"
    db_session.commit()


def test_review_story_is_not_ranked_until_promoted(db_session, monkeypatch):
    _stub_out_production(monkeypatch)
    cand = _raw_id(db_session, CANDIDATE_TITLE, "https://example.test/a")
    rev = _raw_id(db_session, REVIEW_TITLE, "https://example.test/b")

    first = _process(db_session, monkeypatch)

    assert first["classify"] == {"classified": 2, "ai_candidates": 1, "ai_review": 1}
    assert db_session.get(StoryState, rev).ai_relevance == "ai_review"
    assert _episode_story_ids(db_session) == {cand}

    apply_review_decision(db_session, rev, "promote")
    _mark_episode_ready(db_session)
    _process(db_session, monkeypatch)

    assert _episode_story_ids(db_session) == {cand, rev}
    assert db_session.get(StoryState, rev).review_decision == "promoted"


def test_rejected_review_story_never_reaches_ranking(db_session, monkeypatch):
    _stub_out_production(monkeypatch)
    cand = _raw_id(db_session, CANDIDATE_TITLE, "https://example.test/a")
    rev = _raw_id(db_session, REVIEW_TITLE, "https://example.test/b")
    _process(db_session, monkeypatch)

    apply_review_decision(db_session, rev, "reject")
    _mark_episode_ready(db_session)
    _process(db_session, monkeypatch)

    assert _episode_story_ids(db_session) == {cand}
    assert db_session.get(StoryState, rev).ai_relevance == "not_ai"


def test_only_review_stories_means_no_episode_stories(db_session, monkeypatch):
    _stub_out_production(monkeypatch)
    rev = _raw_id(db_session, REVIEW_TITLE, "https://example.test/b")

    _process(db_session, monkeypatch)

    assert db_session.get(StoryState, rev).ai_relevance == "ai_review"
    assert db_session.query(EpisodeStory).count() == 0


# --- API endpoints --------------------------------------------------------------


@pytest.fixture
def api_db(db_session, monkeypatch):
    monkeypatch.setattr(main, "SessionLocal", lambda: db_session)
    return db_session


def test_review_queue_lists_only_review_stories_with_their_explanation(api_db):
    rev = _review_story(api_db, title=REVIEW_TITLE)
    _review_story(api_db, title="Already a candidate", state="ai_candidate")
    _review_story(api_db, title="Already rejected", state="not_ai")

    body = main.list_review_queue(date="2026-09-20")

    assert body["date"] == "2026-09-20"
    assert body["count"] == 1
    story = body["stories"][0]
    assert story["id"] == rev.id
    assert story["title"] == REVIEW_TITLE
    assert story["reason"].startswith("Review:")
    assert story["classifier_version"] == "rules-v1"
    assert story["ai_relatedness"] == "core"


def test_review_queue_is_scoped_to_the_requested_day(api_db):
    _review_story(api_db, title=REVIEW_TITLE)

    assert main.list_review_queue(date="2026-09-21") == {"date": "2026-09-21", "count": 0, "stories": []}


def test_review_queue_rejects_a_bad_date(api_db):
    with pytest.raises(HTTPException) as err:
        main.list_review_queue(date="not-a-date")
    assert err.value.status_code == 422


def test_review_endpoint_promotes_and_rejects(api_db):
    promote_me = _review_story(api_db, title="Promote me")
    reject_me = _review_story(api_db, title="Reject me")

    promoted = main.review_story(promote_me.id, main.ReviewDecisionRequest(decision="promote"))
    rejected = main.review_story(reject_me.id, main.ReviewDecisionRequest(decision="reject"))

    assert promoted["ai_relevance"] == "ai_candidate"
    assert promoted["review_decision"] == "promoted"
    assert promoted["reviewed_at"] is not None
    assert rejected["ai_relevance"] == "not_ai"
    assert rejected["review_decision"] == "rejected"
    assert main.list_review_queue(date="2026-09-20")["count"] == 0


@pytest.mark.parametrize(
    "story_id_offset, decision, status",
    [(0, "maybe", 422), (999, "promote", 404)],
)
def test_review_endpoint_error_codes(api_db, story_id_offset, decision, status):
    item = _review_story(api_db)

    with pytest.raises(HTTPException) as err:
        main.review_story(item.id + story_id_offset, main.ReviewDecisionRequest(decision=decision))
    assert err.value.status_code == status


def test_review_endpoint_conflicts_when_already_decided(api_db):
    item = _review_story(api_db)
    main.review_story(item.id, main.ReviewDecisionRequest(decision="promote"))

    with pytest.raises(HTTPException) as err:
        main.review_story(item.id, main.ReviewDecisionRequest(decision="reject"))
    assert err.value.status_code == 409
