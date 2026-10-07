"""
Same-day content dedup (app/dedup/same_day.py + the stage in app/tasks/content_dedup.py).

Editorial rule under test: two stories are duplicates only when they report the same news
DEVELOPMENT. Sharing a company, product or topic is not enough, and thin or failed extraction never
produces a duplicate on lexical overlap alone. Scenario tests run the real embedding model (skipped,
loudly, where it cannot be loaded).
"""
import hashlib
from datetime import date, datetime, timedelta, timezone

import pytest

from app.dedup.decision import METHOD_VERSION, decide
from app.dedup.embedder import EmbedderUnavailable, default_embedder
from app.dedup.features import build_features
from app.dedup.same_day import CORROBORATED_RULES, _types_conflict, DayStory, cluster_same_day, judge_pair
from app.models import Episode, EpisodeStory, NewsItem, StoryState
from app.tasks import content_dedup
from tests.test_historical_dedup import (
    DOTS_EXPANDS_NEW_CONTENT,
    DOTS_IMAGES,
    DOTS_LAUNCH,
    DOTS_LAUNCH_REWORDED,
    DOTS_OTHER_PUBLISHER,
    DOTS_PRICING,
    DOTS_SECURITY,
    MUSE,
)

DAY = date(2026, 10, 6)
T0 = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def model():
    try:
        embedder = default_embedder(persist=False)
        embedder.embed(["probe"])
    except EmbedderUnavailable as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"embedding model unavailable: {exc}")
    return embedder


def day_story(story_id, title, body, hours=0, source="Test Source", fetch="success"):
    return DayStory(
        story_id=story_id,
        features=build_features(
            story_id, title, body, None, fetch if body else "fetch_error",
            f"https://example.com/{story_id}", hashlib.sha1(f"{title}{body}".encode()).hexdigest(), source,
        ),
        published_at=T0 + timedelta(hours=hours),
        source_name=source,
        raw_content=body,
        raw_summary=None,
        title=title,
    )


def links(model, stories, pinned=()):
    return cluster_same_day(stories, set(pinned), model).links


# --------------------------------------------------------------------------------------------
# Decision scenarios (real model)
# --------------------------------------------------------------------------------------------

def test_same_event_different_title_is_duplicate(model):
    found = links(model, [
        day_story(1, "OpenAI launches Dots", DOTS_LAUNCH),
        day_story(2, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, hours=2),
    ])
    assert found[2].canonical_id == 1


def test_same_event_very_different_wording_is_duplicate(model):
    found = links(model, [
        day_story(1, "OpenAI launches Dots", DOTS_LAUNCH),
        day_story(2, "OpenAI introduces Dots", DOTS_LAUNCH_REWORDED, hours=3),
    ])
    assert 2 in found and found[2].canonical_id == 1


@pytest.mark.parametrize("title,body", [
    ("Researchers find prompt-injection flaw in OpenAI's Dots", DOTS_SECURITY),
    ("OpenAI announces Dots pricing", DOTS_PRICING),
])
def test_same_company_different_development_is_new(model, title, body):
    assert links(model, [day_story(1, "OpenAI launches Dots", DOTS_LAUNCH), day_story(2, title, body, hours=2)]) == {}


def test_same_product_different_development_is_new(model):
    assert links(model, [
        day_story(1, "OpenAI launches Dots", DOTS_LAUNCH),
        day_story(2, "OpenAI adds image generation to Dots", DOTS_IMAGES, hours=2),
    ]) == {}


def test_new_material_development_is_new(model):
    assert links(model, [
        day_story(1, "OpenAI launches Dots", DOTS_LAUNCH),
        day_story(2, "OpenAI expands Dots with a connectors marketplace", DOTS_EXPANDS_NEW_CONTENT, hours=4),
    ]) == {}


def test_different_company_is_new(model):
    assert links(model, [day_story(1, "OpenAI launches Dots", DOTS_LAUNCH), day_story(2, "Meta launches Muse", MUSE, hours=1)]) == {}


# --------------------------------------------------------------------------------------------
# Thin / failed content stays conservative
# --------------------------------------------------------------------------------------------

def test_failed_extraction_never_makes_a_false_duplicate(model):
    failed = day_story(2, "OpenAI has some Dots news", None, hours=2, fetch="fetch_error")
    assert links(model, [day_story(1, "OpenAI launches Dots", DOTS_LAUNCH), failed]) == {}


def test_title_only_stories_are_not_merged_on_lexical_overlap():
    old = day_story(1, "OpenAI launches Dots", None).features
    new = day_story(2, "OpenAI Dots: what we know so far", None, hours=1).features
    verdict = judge_pair(new, old, semantic=0.78, title_similarity=0.80, tfidf=0.95)
    assert not verdict.duplicate


def test_lexical_overlap_alone_never_confirms_a_duplicate():
    old = day_story(1, "OpenAI announces Dots pricing", DOTS_PRICING).features
    new = day_story(2, "Researchers find prompt-injection flaw in OpenAI's Dots", DOTS_SECURITY, hours=1).features
    assert not judge_pair(new, old, semantic=0.78, title_similarity=0.6, tfidf=0.90).duplicate


def test_partial_extraction_is_not_corroborated_by_overlap():
    summary_only = build_features(1, "OpenAI Dots arrive", None, "OpenAI launched Dots for ChatGPT users.", "fetch_error",
                                  "https://example.com/1", None, "Test Source")
    full = day_story(2, "OpenAI unveils Dots agents", DOTS_OTHER_PUBLISHER, hours=1).features
    assert not judge_pair(full, summary_only, semantic=0.82, title_similarity=0.6, tfidf=0.9).duplicate


def test_corroborated_overlap_needs_full_text_on_both_sides():
    filler = " ".join(f"detail{n}" for n in range(130))
    a = day_story(1, "OpenAI rolls out something new", DOTS_LAUNCH + " " + filler).features
    b = day_story(2, "Dots: OpenAI's agents explained", DOTS_OTHER_PUBLISHER + " " + filler, hours=1).features
    assert a.article.quality == "full" and b.article.quality == "full"
    d = decide(b, a, 0.82, 0.55)
    verdict = judge_pair(b, a, 0.82, 0.55, tfidf=0.5)
    if d.rule in CORROBORATED_RULES and not d.is_duplicate:
        assert verdict.duplicate and verdict.rule == "corroborated_overlap"
        assert not judge_pair(b, a, 0.82, 0.55, tfidf=0.10).duplicate
        assert not judge_pair(b, a, 0.75, 0.55, tfidf=0.5).duplicate
    thin = day_story(3, "OpenAI rolls out something new", DOTS_LAUNCH).features
    assert not judge_pair(b, thin, 0.82, 0.55, tfidf=0.5).rule == "corroborated_overlap"


def test_same_product_with_disagreeing_development_types_is_never_corroborated():
    from dataclasses import replace
    filler = " ".join(f"detail{n}" for n in range(130))
    a = day_story(1, "OpenAI rolls out something new", DOTS_LAUNCH + " " + filler).features
    b = day_story(2, "Dots: OpenAI's agents explained", DOTS_OTHER_PUBLISHER + " " + filler, hours=1).features
    a, b = replace(a, types=("launch",)), replace(b, types=("expansion",))
    assert _types_conflict(b, a)
    assert not judge_pair(b, a, 0.84, 0.76, tfidf=0.45).duplicate
    assert not _types_conflict(replace(b, types=("other",)), a)


MISTRAL_BODY = (
    "French AI lab Mistral AI has released Mistral Large 4, a new large multimodal model aiming to "
    "leapfrog both American and Chinese rivals. The model has one trillion parameters and is available "
    "through Mistral's API and on Hugging Face. Mistral said the release closes much of the gap to "
    "closed frontier models on reasoning and coding benchmarks."
)


def test_bare_headline_without_text_is_the_same_report_as_an_article_that_names_it():
    thin = day_story(1, "Mistral Large 4", None, hours=0, source="mistral.ai").features
    full = day_story(2, "Mistral's new 1T model aims to leapfrog closed and open rivals", MISTRAL_BODY,
                     hours=1, source="TechCrunch").features
    for new, old in ((full, thin), (thin, full)):
        verdict = judge_pair(new, old, semantic=0.73, title_similarity=0.67, tfidf=0.0)
        assert verdict.duplicate and verdict.rule == "bare_headline_report"


def test_bare_headline_rule_needs_the_subject_in_the_other_stories_lead():
    thin = day_story(1, "Mistral Large 4", None, source="mistral.ai").features
    other = day_story(2, "Mistral raises funding", "Mistral AI raised a new funding round led by investors "
                      "on Tuesday. The company is valued at billions of dollars.", hours=1).features
    assert not judge_pair(other, thin, semantic=0.73, title_similarity=0.5, tfidf=0.0).duplicate


def test_bare_headline_rule_ignores_headlines_that_state_a_development():
    thin = day_story(1, "Mistral releases Large 4", None, source="mistral.ai").features
    full = day_story(2, "Mistral's new model", MISTRAL_BODY, hours=1).features
    assert judge_pair(full, thin, semantic=0.73, title_similarity=0.6, tfidf=0.0).rule != "bare_headline_report"


def test_bare_headline_rule_needs_text_on_the_other_side():
    a = day_story(1, "Mistral Large 4", None, source="mistral.ai").features
    b = day_story(2, "Mistral Large 4", None, hours=1).features
    assert judge_pair(b, a, semantic=0.9, title_similarity=0.95, tfidf=0.0).rule != "bare_headline_report"


def test_article_with_text_becomes_canonical_over_an_earlier_headline_only_story(model):
    thin = day_story(1, "Mistral Large 4", None, hours=0, source="mistral.ai")
    full = day_story(2, "Mistral's new 1T model aims to leapfrog closed and open rivals", MISTRAL_BODY,
                     hours=1, source="Unknown Blog")
    result = cluster_same_day([thin, full], set(), model)
    assert result.links[1].canonical_id == 2 and 2 not in result.links and result.demoted == {1}


def test_pinned_headline_only_story_is_kept_and_the_article_links_to_it(model):
    thin = day_story(1, "Mistral Large 4", None, hours=0, source="mistral.ai")
    full = day_story(2, "Mistral's new 1T model aims to leapfrog closed and open rivals", MISTRAL_BODY,
                     hours=1, source="TechCrunch")
    result = cluster_same_day([thin, full], {1}, model)
    assert result.links[2].canonical_id == 1 and not result.demoted


# --------------------------------------------------------------------------------------------
# Pool, canonical and election behaviour
# --------------------------------------------------------------------------------------------

def test_copy_of_a_story_already_in_the_episode_is_a_duplicate_of_it(model):
    primary = day_story(1, "OpenAI launches Dots", DOTS_LAUNCH, hours=0)
    copy = day_story(2, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, hours=3)
    found = links(model, [primary, copy], pinned=[1])
    assert found[2].canonical_id == 1 and 1 not in found


def test_pinned_story_is_never_demoted_even_when_a_better_ranked_copy_exists(model):
    pinned = day_story(1, "OpenAI launches Dots", DOTS_LAUNCH, hours=0, source="Unknown Blog")
    better = day_story(2, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, hours=20, source="The Verge")
    result = cluster_same_day([pinned, better], {1}, model)
    assert result.links[2].canonical_id == 1 and not result.demoted


def test_better_ranked_copy_becomes_canonical_and_nothing_chains(model):
    old = day_story(1, "OpenAI launches Dots", DOTS_LAUNCH, hours=0, source="Unknown Blog")
    mid = day_story(2, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, hours=1, source="Unknown Blog")
    new = day_story(3, "OpenAI introduces Dots", DOTS_LAUNCH_REWORDED, hours=22, source="The Verge")
    result = cluster_same_day([old, mid, new], set(), model)
    assert result.demoted == {1}
    assert 3 not in result.links
    assert result.links[1].canonical_id == 3 and result.links[2].canonical_id == 3
    assert all(l.canonical_id not in result.links for l in result.links.values())


def test_equal_ranking_keeps_the_earlier_story_canonical(model):
    a = day_story(1, "OpenAI launches Dots", DOTS_LAUNCH, hours=0)
    b = day_story(2, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, hours=1)
    result = cluster_same_day([a, b], set(), model)
    assert result.links[2].canonical_id == 1 and not result.demoted


def test_stories_outside_the_time_window_are_not_compared(model):
    a = day_story(1, "OpenAI launches Dots", DOTS_LAUNCH, hours=0)
    b = day_story(2, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, hours=60)
    assert links(model, [a, b]) == {}


def test_reason_is_versioned_and_explainable(model):
    found = links(model, [
        day_story(1, "OpenAI launches Dots", DOTS_LAUNCH),
        day_story(2, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, hours=2),
    ])
    text = found[2].dedup_reason()
    assert text.startswith("same_day_v3[") and "semantic=" in text and "matched_against_story_id=1" in text


# --------------------------------------------------------------------------------------------
# The stage (database-backed): persistence, idempotency, stability, embedder outage
# --------------------------------------------------------------------------------------------

def add(db, title, body, hours=0, source="Test Source", fetch="success"):
    item = NewsItem(
        title=title,
        canonical_url=f"https://example.com/{abs(hash((title, hours)))}",
        source_name=source,
        source_type="rss",
        published_at=T0 + timedelta(hours=hours),
        collected_at=T0,
        collection_date=DAY,
        raw_content=body,
        raw_summary=None,
        content_hash=hashlib.sha1(f"{title}{body}".encode()).hexdigest(),
        status="collected",
    )
    db.add(item)
    db.flush()
    db.add(StoryState(id=item.id, ai_relevance="ai_candidate", ai_relevance_score=0.9,
                      content_fetch_status=fetch if body else "fetch_error"))
    db.commit()
    return item.id


def pin(db, story_id, status="primary", position=1):
    episode = db.query(Episode).filter(Episode.episode_date == DAY).first()
    if episode is None:
        episode = Episode(episode_date=DAY, status="draft")
        db.add(episode)
        db.flush()
    db.add(EpisodeStory(episode_id=episode.id, story_id=story_id, rank_position=position,
                        selection_status=status, rank_score=1.0, rank_reason="test"))
    db.commit()


def canon(db, story_id):
    db.expire_all()
    return db.get(StoryState, story_id).canonical_story_id


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(content_dedup.time, "sleep", lambda s: None)


def test_stage_marks_duplicates_and_leaves_new_developments(db_session, model, no_network):
    a = add(db_session, "OpenAI launches Dots", DOTS_LAUNCH, 0)
    b = add(db_session, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, 2)
    c = add(db_session, "OpenAI announces Dots pricing", DOTS_PRICING, 3)
    result = content_dedup.enrich_and_dedup_by_content(db_session, DAY, embedder=model)
    assert canon(db_session, b) == a and canon(db_session, a) is None and canon(db_session, c) is None
    assert result["content_duplicates_found"] == 1
    assert db_session.get(StoryState, b).dedup_reason.startswith("same_day_v3[")


def test_stage_rerun_is_idempotent(db_session, model, no_network):
    a = add(db_session, "OpenAI launches Dots", DOTS_LAUNCH, 0)
    b = add(db_session, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, 2)
    add(db_session, "OpenAI announces Dots pricing", DOTS_PRICING, 3)
    content_dedup.enrich_and_dedup_by_content(db_session, DAY, embedder=model)
    reason = db_session.get(StoryState, b).dedup_reason
    again = content_dedup.enrich_and_dedup_by_content(db_session, DAY, embedder=model)
    assert again["content_duplicates_found"] == 0
    assert canon(db_session, b) == a
    assert db_session.get(StoryState, b).dedup_reason == reason


def test_canonical_relationship_is_stable_when_a_third_copy_arrives(db_session, model, no_network):
    a = add(db_session, "OpenAI launches Dots", DOTS_LAUNCH, 0)
    b = add(db_session, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, 1)
    content_dedup.enrich_and_dedup_by_content(db_session, DAY, embedder=model)
    c = add(db_session, "OpenAI introduces Dots", DOTS_LAUNCH_REWORDED, 2)
    content_dedup.enrich_and_dedup_by_content(db_session, DAY, embedder=model)
    assert canon(db_session, b) == a and canon(db_session, c) == a and canon(db_session, a) is None


def test_stage_keeps_the_episode_primary_and_flags_a_second_copy(db_session, model, no_network):
    primary = add(db_session, "OpenAI launches Dots", DOTS_LAUNCH, 0)
    pin(db_session, primary)
    copy = add(db_session, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, 3)
    content_dedup.enrich_and_dedup_by_content(db_session, DAY, embedder=model)
    assert canon(db_session, copy) == primary and canon(db_session, primary) is None
    state = db_session.get(StoryState, primary)
    assert state.source_sufficiency is None  # a pinned primary is only compared, never re-assessed or edited


def test_stage_repoints_earlier_duplicates_when_a_better_copy_takes_over(db_session, model, no_network):
    old = add(db_session, "OpenAI launches Dots", DOTS_LAUNCH, 0, source="Unknown Blog")
    mid = add(db_session, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, 1, source="Unknown Blog")
    content_dedup.enrich_and_dedup_by_content(db_session, DAY, embedder=model)
    assert canon(db_session, mid) == old
    new = add(db_session, "OpenAI introduces Dots", DOTS_LAUNCH_REWORDED, 22, source="The Verge")
    content_dedup.enrich_and_dedup_by_content(db_session, DAY, embedder=model)
    assert canon(db_session, new) is None
    assert canon(db_session, old) == new and canon(db_session, mid) == new


def test_stage_skips_cleanly_when_the_embedder_is_unavailable(db_session, no_network):
    class Broken:
        name = "broken"

        def embed(self, texts):
            raise EmbedderUnavailable("no model")

    a = add(db_session, "OpenAI launches Dots", DOTS_LAUNCH, 0)
    b = add(db_session, "OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER, 2)
    result = content_dedup.enrich_and_dedup_by_content(db_session, DAY, embedder=Broken())
    assert result["same_day_skipped_reason"] and result["content_duplicates_found"] == 0
    assert canon(db_session, a) is None and canon(db_session, b) is None


# --------------------------------------------------------------------------------------------
# Historical dedup is untouched
# --------------------------------------------------------------------------------------------

def test_historical_method_version_and_rules_unchanged(model):
    assert METHOD_VERSION == "semantic-v1"
    old = day_story(1, "OpenAI launches Dots", DOTS_LAUNCH).features
    new = day_story(2, "OpenAI announces Dots pricing", DOTS_PRICING, hours=1).features
    ov, nv = model.embed([old.embed_text, new.embed_text])
    ot, nt = model.embed([old.title, new.title])
    d = decide(new, old, float(ov @ nv), float(ot @ nt))
    assert d.decision == "new_development"
