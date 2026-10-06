"""
Ranking's AI-relevance input: rows with a persisted classifier result are ranked on that result,
not on the legacy keyword score; rows without one keep ranking exactly as before.
"""
from datetime import date, datetime, timedelta

import pytest

from app.config import settings
from app.models import NewsItem, StoryState
from app.ranking.engine import (
    CLASSIFIED_AI_RELEVANCE,
    SCORE_WEIGHTS,
    VERIFICATION_BONUS,
    ai_relevance_input,
    compute_total_score,
)
from app.tasks.ranking import _score_and_select_top_stories

DAY = date(2026, 9, 20)
# Naive on purpose: SQLite hands published_at back without tzinfo (see tests/test_processing_flow.py).
NOW = datetime(2026, 9, 20, 12, 0)
PUBLISHED = NOW - timedelta(hours=2)
WINDOW_HOURS = 24.0


def _total(ai_input, source="OpenAI", published=PUBLISHED, dups=0, verification="pending"):
    return compute_total_score(
        published_at=published,
        source_name=source,
        ai_relevance_score=ai_input,
        duplicate_count=dups,
        now=NOW,
        window_hours=WINDOW_HOURS,
        verification_status=verification,
    )[0]


# --- ai_relevance_input ---------------------------------------------------------


@pytest.mark.parametrize("score", [0.0, 0.4, 0.5, 0.6, 0.9])
def test_rules_candidate_ignores_the_legacy_keyword_score(score):
    assert (
        ai_relevance_input(score, classifier_version="rules-v1", classifier_disposition="candidate")
        == CLASSIFIED_AI_RELEVANCE
    )


@pytest.mark.parametrize("score", [0.0, 0.4, 0.9])
def test_promoted_review_story_ignores_the_legacy_keyword_score(score):
    assert (
        ai_relevance_input(
            score,
            classifier_version="rules-v1",
            classifier_disposition="review",
            review_decision="promoted",
        )
        == CLASSIFIED_AI_RELEVANCE
    )


@pytest.mark.parametrize("score", [None, 0.0, 0.4, 0.6, 1.0])
def test_legacy_row_without_classifier_result_keeps_its_score_unchanged(score):
    assert ai_relevance_input(score) == score
    assert ai_relevance_input(score, None, None, None) == score


@pytest.mark.parametrize(
    "disposition, decision",
    [("review", None), ("reject", None), ("review", "rejected")],
)
def test_review_and_reject_rows_get_no_certification(disposition, decision):
    # These never reach ranking (the pool requires ai_relevance == "ai_candidate"); if one ever
    # did, it must not be certified as AI.
    assert (
        ai_relevance_input(
            0.4,
            classifier_version="rules-v1",
            classifier_disposition=disposition,
            review_decision=decision,
        )
        == 0.4
    )


def test_component_is_a_full_relevance_value_not_a_floor():
    # Not max(legacy, floor): the rules value applies even where it is above the legacy score
    # and the legacy score is never consulted.
    assert CLASSIFIED_AI_RELEVANCE == 1.0
    low = ai_relevance_input(0.0, classifier_version="rules-v1", classifier_disposition="candidate")
    high = ai_relevance_input(0.9, classifier_version="rules-v1", classifier_disposition="candidate")
    assert low == high


# --- effect on the total score --------------------------------------------------


def test_rules_candidate_with_zero_legacy_score_ranks_like_one_with_a_high_score():
    zero = ai_relevance_input(0.0, classifier_version="rules-v1", classifier_disposition="candidate")
    high = ai_relevance_input(0.8, classifier_version="rules-v1", classifier_disposition="candidate")
    assert _total(zero) == _total(high)


def test_rules_candidate_with_04_legacy_score_is_not_penalised_against_a_06_candidate():
    four = ai_relevance_input(0.4, classifier_version="rules-v1", classifier_disposition="candidate")
    six = ai_relevance_input(0.6, classifier_version="rules-v1", classifier_disposition="candidate")
    assert _total(four) == _total(six)


def test_legacy_row_total_is_the_old_formula_and_still_graded_by_its_score():
    assert _total(ai_relevance_input(0.4)) == _total(0.4)
    assert _total(ai_relevance_input(0.9)) - _total(ai_relevance_input(0.4)) == pytest.approx(
        SCORE_WEIGHTS["ai_relevance"] * 0.5
    )


def test_the_only_ranking_change_is_the_ai_component():
    # Everything else for a rules candidate is scored exactly as for any other story: the
    # difference to the legacy-score total is the ai-weight times the change in the component.
    ai = ai_relevance_input(0.4, classifier_version="rules-v1", classifier_disposition="candidate")
    assert _total(ai) - _total(0.4) == pytest.approx(
        SCORE_WEIGHTS["ai_relevance"] * (CLASSIFIED_AI_RELEVANCE - 0.4)
    )
    assert sum(SCORE_WEIGHTS.values()) == pytest.approx(1.0)


def test_recency_credibility_momentum_and_verification_still_move_the_total():
    ai = ai_relevance_input(0.0, classifier_version="rules-v1", classifier_disposition="candidate")

    assert _total(ai, published=NOW - timedelta(hours=1)) > _total(ai, published=NOW - timedelta(hours=20))
    assert _total(ai, source="OpenAI") > _total(ai, source="Some Unrated Blog")
    assert _total(ai, dups=4) > _total(ai, dups=0)
    assert _total(ai, verification="verified") - _total(ai) == pytest.approx(VERIFICATION_BONUS)


# --- through the real selection query -------------------------------------------


def _story(
    db,
    title,
    *,
    state="ai_candidate",
    score=0.6,
    version="rules-v1",
    disposition="candidate",
    decision=None,
    source="Test Source",
    published=PUBLISHED,
):
    item = NewsItem(
        title=title,
        canonical_url=f"https://e.test/{abs(hash(title))}",
        source_name=source,
        source_type="rss",
        published_at=published,
        collected_at=NOW,
        collection_date=DAY,
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
            classifier_version=version,
            classifier_disposition=disposition,
            review_decision=decision,
        )
    )
    db.commit()
    return item.id


def _ranked(db):
    scored, top, _, _ = _score_and_select_top_stories(db, NOW, DAY)
    return [item.id for item, _, _, _ in scored], {item.id: s for item, _, s, _ in scored}, top


def test_existing_legacy_candidate_scores_exactly_as_before(db_session):
    story = _story(db_session, "Legacy story", version=None, disposition=None, score=0.7)

    _, scores, _ = _ranked(db_session)

    expected, _ = compute_total_score(
        published_at=PUBLISHED,
        source_name="Test Source",
        ai_relevance_score=0.7,
        duplicate_count=0,
        now=NOW,
        window_hours=settings.news_window_hours,
    )
    assert scores[story] == pytest.approx(expected)


def test_discovered_candidates_are_not_ranked_below_equal_stories_the_old_list_recognised(db_session):
    # Same source, time and momentum; only the legacy keyword score differs. The ChatGPT/Copilot
    # style stories scored 0.0 and 0.4 because the old keyword list missed their terminology.
    recognised = _story(db_session, "Recognised terms", score=0.6)
    missed_zero = _story(db_session, "ChatGPT gets a new feature", score=0.0)
    missed_partial = _story(db_session, "Copilot summary-only story", score=0.4)

    _, scores, _ = _ranked(db_session)

    assert scores[missed_zero] == pytest.approx(scores[recognised])
    assert scores[missed_partial] == pytest.approx(scores[recognised])


def test_promoted_review_story_ranks_on_the_classification_not_its_keyword_score(db_session):
    promoted = _story(
        db_session, "Promoted", disposition="review", decision="promoted", score=0.0
    )
    candidate = _story(db_session, "Candidate", score=0.8)

    _, scores, _ = _ranked(db_session)

    assert scores[promoted] == pytest.approx(scores[candidate])


def test_review_and_rejected_stories_stay_outside_ranking(db_session):
    cand = _story(db_session, "Candidate")
    _story(db_session, "Awaiting review", state="ai_review", disposition="review", score=0.9)
    _story(db_session, "Rejected by classifier", state="not_ai", disposition="reject", score=0.9)
    _story(
        db_session,
        "Rejected by editor",
        state="not_ai",
        disposition="review",
        decision="rejected",
        score=0.9,
    )

    order, _, top = _ranked(db_session)

    assert order == [cand]
    assert [item.id for item, _, _, _ in top] == [cand]


def test_legacy_and_rules_rows_rank_together_with_other_factors_unchanged(db_session):
    old_fresh = _story(db_session, "Old fresh", version=None, disposition=None, score=0.4,
                       published=NOW - timedelta(hours=1))
    new_fresh = _story(db_session, "New fresh", score=0.0, published=NOW - timedelta(hours=1))
    new_stale = _story(db_session, "New stale", score=0.0, published=NOW - timedelta(hours=20))

    order, scores, _ = _ranked(db_session)

    # Same recency/source: the rules candidate beats the legacy 0.4 row; recency still dominates.
    assert order == [new_fresh, old_fresh, new_stale]
    assert scores[new_fresh] > scores[old_fresh] > scores[new_stale]


def test_the_stored_keyword_score_is_not_modified_by_ranking(db_session):
    story = _story(db_session, "Perplexity launches browser", score=0.0)

    _ranked(db_session)
    db_session.expire_all()

    assert db_session.get(StoryState, story).ai_relevance_score == 0.0
