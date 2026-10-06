"""
Ranking at event level: a development must not vanish because its canonical story lost the cutoff,
corroboration counts independent outlets (not same-publisher copies), feed names resolve to the
publisher's credibility, and equal scores order deterministically.
"""
from datetime import date, datetime, timedelta

import pytest

from app.config import settings
from app.models import Episode, EpisodeStory, NewsItem, StoryState
from app.ranking.engine import (
    DEFAULT_CREDIBILITY,
    EventMember,
    VERIFICATION_BONUS,
    compute_credibility_score,
    compute_total_score,
    score_event,
)
from app.sources.registry import NEWS_SOURCES
from app.tasks.ranking import _score_and_select_top_stories, _update_draft_episode

DAY = date(2026, 9, 20)
NOW = datetime(2026, 9, 20, 12, 0)  # naive: SQLite returns published_at without tzinfo
W = settings.news_window_hours


def _member(story_id, source, hours_ago, status="pending", viable=True):
    return EventMember(story_id, source, NOW - timedelta(hours=hours_ago), 1.0, status, viable)


def _own(m, outlets=0):
    return compute_total_score(m.published_at, m.source_name, m.ai_relevance, outlets, NOW, W, m.verification_status)[0]


# --- credibility -------------------------------------------------------------------


def test_registry_feed_names_score_like_the_publisher():
    assert compute_credibility_score("The Guardian AI") == compute_credibility_score("The Guardian") == 0.85
    assert (
        compute_credibility_score("IEEE Spectrum — Artificial Intelligence")
        == compute_credibility_score("IEEE Spectrum")
        == 0.90
    )


def test_first_party_research_blogs_are_rated_like_microsoft_research():
    for name in ("Google Research Blog", "Amazon Science", "Berkeley AI Research (BAIR)"):
        assert compute_credibility_score(name) == compute_credibility_score("Microsoft Research Blog")


def test_unrated_source_gets_the_neutral_default_not_a_penalty():
    assert compute_credibility_score("Some New Outlet") == DEFAULT_CREDIBILITY == 0.60


def test_every_registry_source_resolves_without_error():
    for source in NEWS_SOURCES:
        assert 0.0 < compute_credibility_score(source["name"]) <= 1.0


# --- score_event -------------------------------------------------------------------


def test_event_without_duplicates_scores_exactly_like_a_single_story():
    c = _member(1, "OpenAI", 3, "verified")
    assert score_event(c, [], NOW, W).total == pytest.approx(_own(c))


def test_same_publisher_copy_adds_no_momentum():
    c = _member(1, "Some Blog", 3)
    same = _member(2, "some blog", 5)
    assert score_event(c, [same], NOW, W).total == pytest.approx(_own(c))


def test_independent_outlet_adds_momentum():
    c = _member(1, "Some Blog", 3)
    other = _member(2, "Other Blog", 9)
    ev = score_event(c, [other], NOW, W)
    assert ev.representative_id == 1
    assert ev.total == pytest.approx(_own(c, outlets=1))


def test_newer_duplicate_represents_the_event_and_the_reason_says_so():
    old = _member(1, "TechCrunch AI", 20)
    new = _member(2, "TechRadar", 1)
    ev = score_event(old, [new], NOW, W)
    assert ev.representative_id == 2
    assert ev.total > _own(old, outlets=1)
    assert "duplicate #2" in ev.reason and "canonical #1" in ev.reason


def test_canonical_wins_an_exact_tie():
    a = _member(5, "Blog A", 3, "verified")  # as Verification leaves a corroborated canonical
    b = _member(2, "Blog B", 3)
    assert score_event(a, [b], NOW, W).representative_id == 5


def test_non_viable_duplicate_counts_as_an_outlet_but_cannot_represent():
    old = _member(1, "TechCrunch AI", 20)
    fresh_but_unusable = _member(2, "TechRadar", 1, viable=False)
    ev = score_event(old, [fresh_but_unusable], NOW, W)
    assert ev.representative_id == 1
    assert ev.total == pytest.approx(_own(old, outlets=1))


def test_duplicate_representative_gets_the_verification_bonus_it_earns():
    old = _member(1, "Some Blog", 20)
    new = _member(2, "Other Blog", 1)
    ev = score_event(old, [new], NOW, W)
    assert ev.total == pytest.approx(_own(new, outlets=1) + VERIFICATION_BONUS)


# --- through the real selection query ----------------------------------------------


def _story(db, title, hours_ago, source="Test Source", canonical=None, state="ai_candidate"):
    item = NewsItem(
        title=title,
        canonical_url=f"https://e.test/{abs(hash(title))}",
        source_name=source,
        source_type="rss",
        published_at=NOW - timedelta(hours=hours_ago),
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
            ai_relevance_score=0.6,
            classifier_version="rules-v3",
            classifier_disposition="candidate",
            canonical_story_id=canonical,
        )
    )
    db.commit()
    return item.id


def _ranked(db):
    scored, top, _, _ = _score_and_select_top_stories(db, NOW, DAY)
    return [item.id for item, _, _, _ in scored], top


def test_event_survives_when_its_canonical_alone_would_be_cut(db_session):
    for i in range(30):
        _story(db_session, f"Filler {i}", hours_ago=10)
    canonical = _story(db_session, "Old canonical report", hours_ago=20, source="TechCrunch AI")
    _story(db_session, "Fresh coverage of the same event", hours_ago=1, source="TechRadar", canonical=canonical)

    order, top = _ranked(db_session)

    assert canonical in [item.id for item, _, _, _ in top]
    assert order.index(canonical) < 30


def test_event_without_a_stronger_duplicate_is_still_cut_normally(db_session):
    for i in range(30):
        _story(db_session, f"Filler {i}", hours_ago=10)
    canonical = _story(db_session, "Old canonical report", hours_ago=20, source="TechCrunch AI")
    _story(db_session, "Older copy", hours_ago=21, source="TechRadar", canonical=canonical)

    order, _ = _ranked(db_session)

    assert order.index(canonical) >= 30


def test_ineligible_duplicate_cannot_lift_its_canonical(db_session):
    for i in range(30):
        _story(db_session, f"Filler {i}", hours_ago=10)
    canonical = _story(db_session, "Old canonical report", hours_ago=20, source="TechCrunch AI")
    _story(db_session, "Fresh but not AI", hours_ago=1, source="TechRadar", canonical=canonical, state="not_ai")

    order, _ = _ranked(db_session)

    assert order.index(canonical) >= 30


def test_only_canonicals_are_selected_never_duplicates(db_session):
    canonical = _story(db_session, "Canonical", hours_ago=20)
    dup = _story(db_session, "Duplicate", hours_ago=1, source="TechRadar", canonical=canonical)

    order, _ = _ranked(db_session)

    assert order == [canonical] and dup not in order


def test_equal_scores_order_by_story_id_and_are_stable(db_session):
    ids = [_story(db_session, f"Same score {i}", hours_ago=4) for i in range(6)]

    first, _ = _ranked(db_session)
    second, _ = _ranked(db_session)

    assert first == ids == second


# --- draft top-up ------------------------------------------------------------------


def test_draft_top_up_does_not_add_an_event_the_episode_already_holds(db_session):
    canonical = _story(db_session, "Canonical report", hours_ago=2)
    held = _story(db_session, "Same event, held by the draft", hours_ago=3, source="TechRadar", canonical=canonical)
    other = _story(db_session, "Unrelated story", hours_ago=2)
    episode = Episode(episode_date=DAY, status="draft")
    db_session.add(episode)
    db_session.flush()
    db_session.add(EpisodeStory(
        episode_id=episode.id, story_id=held, rank_position=1,
        selection_status="primary", rank_score=1.0, rank_reason="kept",
    ))
    db_session.commit()

    _update_draft_episode(db_session, episode, NOW)

    story_ids = {r.story_id for r in db_session.query(EpisodeStory).filter_by(episode_id=episode.id)}
    assert story_ids == {held, other}
