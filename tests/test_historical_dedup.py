"""
Historical duplicate detector (app/dedup): decision rules, retrieval, persistence and the stage.

Scenario tests (1-9) run the real embedding model, because the point is whether paraphrases and
look-alikes land on the right side of the similarity thresholds; they are skipped, loudly, where
the model cannot be loaded. The rule tests, idempotency, missing-content and canonical_story_id
tests never need it (they use a deterministic fake embedder or explicit similarities).
"""
import hashlib
import re
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from app.dedup.decision import DUPLICATE, NEW_DEVELOPMENT, decide
from app.dedup.detector import Stats, build_corpus, detect_stories
from app.dedup.embedder import EmbedderUnavailable, default_embedder
from app.dedup.features import build_features
from app.dedup.relations import effective_duplicate_story_ids, save_verdict
from app.models import Episode, EpisodeStory, HistoricalStoryRelation, NewsItem, StoryState
from app.tasks.historical_dedup import deduplicate_against_history
from app.tasks.ranking import _score_and_select_top_stories

TODAY = date(2026, 10, 5)

# --------------------------------------------------------------------------------------------
# Articles
# --------------------------------------------------------------------------------------------

DOTS_LAUNCH = (
    "OpenAI launched Dots on Tuesday, a new family of always-on AI agents for ChatGPT users. "
    "Dots complete multistep tasks on a user's behalf and connect to apps such as calendar and email. "
    "The agents are powered by the GPT-6 Astra model. "
    "OpenAI said Dots are available in preview to ChatGPT Plus subscribers starting today."
)
DOTS_LAUNCH_REWORDED = (
    "On Tuesday OpenAI introduced Dots, its new always-on AI agents inside ChatGPT. "
    "The agents carry out multistep tasks for the user and plug into calendar and email apps. "
    "They run on the company's GPT-6 Astra model. "
    "A preview is open to ChatGPT Plus subscribers from today, OpenAI said."
)
DOTS_IMAGES = (
    "OpenAI is adding image generation to Dots, the always-on agents it introduced last week. "
    "Users can now ask a Dot to draw diagrams, product mockups and illustrations inside a conversation. "
    "The feature uses OpenAI's image model and renders results in about ten seconds. "
    "Image generation is rolling out to all Dots users this month."
)
DOTS_PRICING = (
    "OpenAI announced pricing for Dots on Thursday. "
    "Dots will cost $20 a month for individuals and $40 per seat for teams. "
    "The preview stays free for ChatGPT Plus subscribers until the end of the month. "
    "Enterprise customers will get volume discounts, according to the company."
)
DOTS_SECURITY = (
    "Security researchers disclosed a prompt-injection vulnerability in OpenAI's Dots agents. "
    "A malicious web page can instruct a Dot to forward a user's private calendar entries to an attacker. "
    "The researchers reported the flaw to OpenAI, which said a fix is being tested. "
    "Users are advised to disable web browsing for their Dots until the patch ships."
)
DOTS_OTHER_PUBLISHER = (
    "OpenAI has unveiled Dots, a line of always-on AI agents that work through tasks for ChatGPT users. "
    "The Dots handle multistep jobs for people and link up with calendar and email. "
    "Under the hood they use OpenAI's GPT-6 Astra model. "
    "Plus subscribers can try a preview starting Tuesday."
)
DOTS_EXPANDS_NEW_CONTENT = (
    "OpenAI is giving Dots the ability to book restaurant tables and manage shared family calendars. "
    "The update adds a new connectors marketplace where outside developers can publish Dots skills. "
    "More than 200 partner apps, including Spotify and Uber, launch with the marketplace. "
    "OpenAI said the expansion reaches all paid users next week."
)
DOTS_EXPANDS_NO_NEW_CONTENT = (
    "OpenAI launched Dots on Tuesday, a new family of always-on AI agents for ChatGPT users. "
    "Dots complete multistep tasks on a user's behalf and connect to apps such as calendar and email. "
    "The agents are powered by the GPT-6 Astra model. "
    "OpenAI said Dots are available in preview to ChatGPT Plus subscribers starting today."
)
MUSE = (
    "Meta launched Muse, an AI agent it says can write emails and buy things online for its users. "
    "Muse runs on Meta's Llama models and lives inside WhatsApp and Instagram. "
    "The company said Muse starts rolling out in the United States this week. "
    "Meta did not say when Muse will reach users in Europe."
)

DOTS_OLD = ("OpenAI launches Dots", DOTS_LAUNCH)


@pytest.fixture(scope="module")
def model():
    try:
        embedder = default_embedder(persist=False)
        embedder.embed(["probe"])
    except EmbedderUnavailable as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"embedding model unavailable: {exc}")
    return embedder


def feats(story_id, title, body, url=None, status="success", source="Test Source"):
    return build_features(
        story_id, title, body, None, status if body else "fetch_error",
        url or f"https://example.com/{story_id}", hashlib.sha1(f"{title}{body}".encode()).hexdigest(), source,
    )


def judge(model, old, new):
    """Run the real model over two (title, body) pairs and apply the decision layer."""
    o, n = feats(1, *old), feats(2, *new)
    ov, nv = model.embed([o.embed_text, n.embed_text])
    ot, nt = model.embed([o.title, n.title])
    return decide(n, o, float(ov @ nv), float(ot @ nt))


# --------------------------------------------------------------------------------------------
# Scenarios against the real model
# --------------------------------------------------------------------------------------------

def test_01_exact_same_article_is_duplicate(model):
    result = judge(model, DOTS_OLD, ("OpenAI launches Dots", DOTS_LAUNCH))
    assert result.decision == DUPLICATE


def test_02_same_event_different_wording_is_duplicate(model):
    result = judge(model, DOTS_OLD, ("OpenAI introduces Dots", DOTS_LAUNCH_REWORDED))
    assert result.decision == DUPLICATE, result.reason


def test_03_same_product_new_feature_is_new_development(model):
    result = judge(model, DOTS_OLD, ("OpenAI adds image generation to Dots", DOTS_IMAGES))
    assert result.decision == NEW_DEVELOPMENT, result.reason


def test_04_new_pricing_is_new_development(model):
    result = judge(model, DOTS_OLD, ("OpenAI announces Dots pricing", DOTS_PRICING))
    assert result.decision == NEW_DEVELOPMENT
    assert result.rule == "new_development_type"


def test_05_security_issue_is_new_development(model):
    result = judge(model, DOTS_OLD, ("Researchers find prompt-injection flaw in OpenAI's Dots", DOTS_SECURITY))
    assert result.decision == NEW_DEVELOPMENT
    assert "security" in result.reason


def test_06_same_launch_by_another_publisher_is_duplicate(model):
    result = judge(model, DOTS_OLD, ("OpenAI unveils Dots, its always-on AI agents", DOTS_OTHER_PUBLISHER))
    assert result.decision == DUPLICATE, result.reason


def test_07_similar_titles_different_development_decided_from_content(model):
    """'OpenAI expands Dots with new capabilities' is a duplicate or new purely by what the article says."""
    title = "OpenAI expands Dots with new capabilities"
    new_content = judge(model, DOTS_OLD, (title, DOTS_EXPANDS_NEW_CONTENT))
    recap = judge(model, DOTS_OLD, (title, DOTS_EXPANDS_NO_NEW_CONTENT))
    assert new_content.decision == NEW_DEVELOPMENT, new_content.reason
    assert recap.decision == DUPLICATE, recap.reason


def test_09_different_company_and_product_is_new_development(model):
    result = judge(model, DOTS_OLD, ("Meta launches Muse", MUSE))
    assert result.decision == NEW_DEVELOPMENT
    assert result.rule in {"unrelated", "different_subject"}


# --------------------------------------------------------------------------------------------
# Rules that must not depend on the model: explicit similarities
# --------------------------------------------------------------------------------------------

def test_rule_identical_source_wins_whatever_the_similarity():
    old = feats(1, "OpenAI launches Dots", DOTS_LAUNCH, url="https://a.com/x")
    new = feats(2, "A different headline entirely", DOTS_IMAGES, url="https://a.com/x")
    assert decide(new, old, 0.1, 0.1).rule == "identical_source"


def test_rule_unrelated_is_not_recorded():
    old = feats(1, "OpenAI launches Dots", DOTS_LAUNCH)
    new = feats(2, "Meta launches Muse", MUSE)
    result = decide(new, old, 0.55, 0.4)
    assert result.decision == NEW_DEVELOPMENT and result.related is False


def test_rule_same_company_is_never_enough():
    old = feats(1, "OpenAI launches Dots", DOTS_LAUNCH)
    new = feats(2, "OpenAI announces Dots pricing", DOTS_PRICING)
    # Even at a near-perfect similarity, a pricing story about a launched product is new.
    assert decide(new, old, 0.93, 0.8).decision == NEW_DEVELOPMENT


def test_rule_first_person_account_is_not_the_announcement():
    hands_on = (
        "Yesterday, I spent the morning at OpenAI's DevDay, where the biggest reveal was Dots. "
        "I wanted to know what it felt like to boss my own Dot around. "
        "OpenAI launched Dots on Tuesday as always-on agents for ChatGPT. "
        "They run on GPT-6 Astra."
    )
    old = feats(1, "OpenAI launches Dots", DOTS_LAUNCH)
    new = feats(2, "The Battle to Be Your Personal AI Agent Is Here", hands_on)
    assert decide(new, old, 0.865, 0.77).rule == "first_hand_account"


def test_rule_explainer_headline_is_not_a_second_report():
    old = feats(1, "OpenAI launches Dots", DOTS_LAUNCH)
    new = feats(2, "Why OpenAI's Dots matter for the agent race", DOTS_LAUNCH_REWORDED)
    assert decide(new, old, 0.86, 0.8).rule == "explainer_coverage"


# --------------------------------------------------------------------------------------------
# Database-backed tests (fake, deterministic embedder)
# --------------------------------------------------------------------------------------------

class HashingEmbedder:
    """Bag-of-words hashing embedder: identical text -> 1.0, mostly shared words -> high, disjoint -> ~0."""

    dim = 256

    def embed(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9']+", text.lower()):
                out[row, int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1.0
            norm = np.linalg.norm(out[row])
            if norm:
                out[row] /= norm
        return out


def add_story(db, title, body, collected, url=None, ai="ai_candidate", fetch="success", canonical=None):
    item = NewsItem(
        title=title,
        canonical_url=url or f"https://example.com/{abs(hash((title, collected)))}",
        source_name="Test Source",
        source_type="rss",
        published_at=datetime.combine(collected, datetime.min.time(), tzinfo=timezone.utc),
        collected_at=datetime.now(timezone.utc),
        collection_date=collected,
        raw_content=body,
        raw_summary=None,
        content_hash=hashlib.sha1(f"{title}{body}".encode()).hexdigest(),
        status="collected",
    )
    db.add(item)
    db.flush()
    db.add(StoryState(
        id=item.id, ai_relevance=ai, ai_relevance_score=0.9,
        content_fetch_status=fetch if body else "fetch_error", canonical_story_id=canonical,
    ))
    db.commit()
    return item.id


def publish(db, story_id, episode_date, status="approved"):
    episode = Episode(episode_date=episode_date, status=status)
    db.add(episode)
    db.flush()
    db.add(EpisodeStory(
        episode_id=episode.id, story_id=story_id, rank_position=1, selection_status="primary", rank_score=1.0
    ))
    db.commit()


def relations(db):
    return db.query(HistoricalStoryRelation).order_by(HistoricalStoryRelation.id).all()


def test_08_duplicate_of_a_story_published_long_ago_is_caught(db_session):
    """No 48-hour window: coverage from 40 days ago still blocks a repeat today."""
    old_id = add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY - timedelta(days=40))
    publish(db_session, old_id, TODAY - timedelta(days=40))
    new_id = add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY, url="https://other.example/dots")

    result = deduplicate_against_history(db_session, TODAY, embedder=HashingEmbedder())

    assert result["corpus_size"] == 1 and result["duplicates"] == 1
    (row,) = relations(db_session)
    assert (row.story_id, row.matched_story_id, row.decision) == (new_id, old_id, DUPLICATE)
    assert row.reason and row.method_version


def test_only_published_primaries_count_as_coverage(db_session):
    """Raw stories, backups and unreviewed drafts are not what the audience was told."""
    raw = add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY - timedelta(days=3))
    draft = add_story(db_session, "OpenAI launches Dots again", DOTS_LAUNCH, TODAY - timedelta(days=2))
    publish(db_session, draft, TODAY - timedelta(days=2), status="draft")
    add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY, url="https://other.example/dots")

    result = deduplicate_against_history(db_session, TODAY, embedder=HashingEmbedder())

    assert raw and result["corpus_size"] == 0 and relations(db_session) == []


def test_10_missing_article_content_is_handled_explicitly(db_session):
    old_id = add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY - timedelta(days=5))
    publish(db_session, old_id, TODAY - timedelta(days=5))
    # Headline-only story (fetch failed, no summary), related by topic but stating no matching development.
    new_id = add_story(db_session, "Dots agents spark debate about workplace automation", None, TODAY)

    corpus = build_corpus(db_session, HashingEmbedder())
    verdicts = detect_stories(db_session, [new_id], corpus, HashingEmbedder(), {new_id: TODAY}, Stats())

    verdict = verdicts[new_id]
    if verdict is not None:  # related enough to compare: it must be judged on the headline alone and kept
        assert verdict.decision.decision == NEW_DEVELOPMENT
        assert verdict.decision.content_basis == "title_only"
    features = feats(new_id, "Dots agents spark debate about workplace automation", None)
    assert features.article.quality == "title_only"


def test_10b_missing_content_never_suppresses_on_weak_evidence():
    old = feats(1, "OpenAI launches Dots", DOTS_LAUNCH)
    new = feats(2, "OpenAI Dots debate heats up", None)
    result = decide(new, old, 0.78, 0.7)
    assert result.decision == NEW_DEVELOPMENT and result.content_basis == "title_only"


def test_11_replay_is_idempotent(db_session):
    old_id = add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY - timedelta(days=4))
    publish(db_session, old_id, TODAY - timedelta(days=4))
    add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY, url="https://other.example/dots")
    add_story(db_session, "OpenAI announces Dots pricing", DOTS_PRICING, TODAY)

    first = deduplicate_against_history(db_session, TODAY, embedder=HashingEmbedder())
    snapshot = [(r.story_id, r.matched_story_id, r.decision, r.reason, r.method_version) for r in relations(db_session)]
    second = deduplicate_against_history(db_session, TODAY, embedder=HashingEmbedder())

    assert first["duplicates"] + first["new_developments"] >= 1
    recorded = first["duplicates"] + first["new_developments"]
    assert second["already_decided"] == recorded  # unrelated stories are not recorded, so they are re-checked
    assert second["duplicates"] == 0 and second["new_developments"] == 0
    assert [(r.story_id, r.matched_story_id, r.decision, r.reason, r.method_version) for r in relations(db_session)] == snapshot


def test_11b_save_verdict_never_overwrites_an_existing_row(db_session):
    old_id = add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY - timedelta(days=4))
    publish(db_session, old_id, TODAY - timedelta(days=4))
    new_id = add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY, url="https://other.example/dots")
    corpus = build_corpus(db_session, HashingEmbedder())
    verdict = detect_stories(db_session, [new_id], corpus, HashingEmbedder(), {new_id: TODAY}, Stats())[new_id]

    assert save_verdict(db_session, verdict) is True
    db_session.commit()
    row = relations(db_session)[0]
    row.editor_override = NEW_DEVELOPMENT  # a future manual override
    db_session.commit()
    assert save_verdict(db_session, verdict) is False
    db_session.commit()
    assert relations(db_session)[0].editor_override == NEW_DEVELOPMENT
    assert effective_duplicate_story_ids(db_session) == set()  # the override outranks the detector


def test_12_canonical_story_id_semantics_unchanged(db_session):
    old_id = add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY - timedelta(days=4))
    publish(db_session, old_id, TODAY - timedelta(days=4))
    canonical = add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY, url="https://other.example/dots")
    same_day_copy = add_story(
        db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY, url="https://third.example/dots", canonical=canonical
    )
    state = db_session.get(StoryState, canonical)
    state.repeats_story_id = None
    db_session.commit()

    deduplicate_against_history(db_session, TODAY, embedder=HashingEmbedder())

    db_session.expire_all()
    assert db_session.get(StoryState, same_day_copy).canonical_story_id == canonical  # untouched
    assert db_session.get(StoryState, canonical).canonical_story_id is None
    assert db_session.get(StoryState, canonical).repeats_story_id is None  # no longer written
    decided = {r.story_id for r in relations(db_session)}
    assert canonical in decided and same_day_copy not in decided  # same-day copies are not candidates


def test_duplicates_leave_the_ranking_pool_and_new_developments_stay(db_session):
    old_id = add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY - timedelta(days=4))
    publish(db_session, old_id, TODAY - timedelta(days=4))
    dup = add_story(db_session, "OpenAI launches Dots", DOTS_LAUNCH, TODAY, url="https://other.example/dots")
    fresh = add_story(db_session, "Meta launches Muse", MUSE, TODAY)
    for sid in (dup, fresh):
        state = db_session.get(StoryState, sid)
        state.source_sufficiency = "sufficient"
    db_session.commit()

    deduplicate_against_history(db_session, TODAY, embedder=HashingEmbedder())
    scored, *_ = _score_and_select_top_stories(db_session, datetime(2026, 10, 5, 12), TODAY)

    pool = {item.id for item, *_ in scored}
    assert dup not in pool and fresh in pool
