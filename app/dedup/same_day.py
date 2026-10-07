"""
Same-day content dedup: is this story the same news DEVELOPMENT as another story collected on the
same day?

Reuses the historical detector's features and rule ladder (`decide`) so "same development" means
the same thing in both places, and adds the one signal the ladder does not have, TF-IDF overlap of
the article text, only as corroboration, never as the sole reason. Thin or failed extraction never
produces a duplicate on lexical overlap alone.

Pure and deterministic: stories in, links out. The stage (app/tasks/content_dedup.py) persists them
on the existing StoryState.canonical_story_id / dedup_reason columns.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from app.config import settings
from app.dedup.decision import RELATED_FLOOR, decide
from app.dedup.embedder import Embedder
from app.dedup.features import StoryFeatures, new_facts
from app.filters.content_similarity import (
    CONTENT_SIMILARITY_THRESHOLD,
    compute_pairwise_cosine_matrix,
    get_comparable_text,
)
from app.filters.dedup import TIME_WINDOW_HOURS
from app.ranking.engine import compute_total_score

SAME_DAY_VERSION = "same-day-v3"

BARE_HEADLINE_RULE = "bare_headline_report"

# A pair the headline rules cannot call (development_unconfirmed) or that agrees on the kind of
# development but falls just short on similarity (insufficient_overlap) is still the same report when
# both sides have full article text, the text overlaps lexically and the semantic similarity is high.
CORROBORATED_RULES = ("development_unconfirmed", "insufficient_overlap")
CORROBORATED_SEMANTIC = 0.80
# A later duplicate takes over as the cluster's canonical only when it would clearly out-rank the
# earlier one on recency + source credibility (the ranking weights), so a tie keeps the older story.
ELECTION_MARGIN = 0.05


@dataclass
class DayStory:
    story_id: int
    features: StoryFeatures
    published_at: datetime | None
    source_name: str
    raw_content: str | None
    raw_summary: str | None
    title: str


@dataclass
class Link:
    story_id: int
    canonical_id: int
    rule: str
    reason: str
    semantic: float
    tfidf: float

    def dedup_reason(self) -> str:
        return (
            f"{SAME_DAY_VERSION.replace('-', '_')}[{self.rule}]: {self.reason} "
            f"(semantic={self.semantic:.2f}, tfidf={self.tfidf:.2f}), "
            f"matched_against_story_id={self.canonical_id}"
        )


@dataclass
class PairVerdict:
    duplicate: bool
    rule: str
    reason: str


@dataclass
class DayResult:
    links: dict[int, Link] = field(default_factory=dict)  # duplicate story id -> link
    demoted: set[int] = field(default_factory=set)  # canonicals displaced by a better-ranked copy
    comparisons: int = 0
    decide_calls: int = 0


def _types_conflict(new: StoryFeatures, old: StoryFeatures) -> bool:
    """Both stories name a development type (headline or lead) and the sets are disjoint: the same
    product covered for a different development (a launch vs an expansion), which wording overlap
    must never merge."""
    n = {t for t in new.types if t != "other"}
    o = {t for t in old.types if t != "other"}
    return bool(n and o and not (n & o))


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _bare_headline_report(thin: StoryFeatures, full: StoryFeatures) -> bool:
    """`thin` has no article text and its whole headline only names a subject ("Mistral Large 4",
    no development verb); `full` has article text and states that subject in its own headline or
    lead. The bare headline carries no development of its own, so it is the same report."""
    if thin.article.quality != "title_only" or full.article.quality == "title_only":
        return False
    if {t for t in thin.types if t != "other"}:
        return False
    name = _normalized(thin.title)
    return len(name.split()) >= 2 and name in _normalized(full.head_lower)


def judge_pair(new: StoryFeatures, old: StoryFeatures, semantic: float, title_similarity: float,
               tfidf: float) -> PairVerdict:
    d = decide(new, old, semantic, title_similarity)
    if d.is_duplicate:
        return PairVerdict(True, d.rule, d.reason)
    if d.related and (_bare_headline_report(new, old) or _bare_headline_report(old, new)):
        return PairVerdict(
            True, BARE_HEADLINE_RULE,
            "One story has no article text and its headline only names a subject the other story "
            "reports on, so it adds no development of its own.",
        )
    if (
        d.rule in CORROBORATED_RULES
        and d.content_basis == "full"
        and semantic >= CORROBORATED_SEMANTIC
        and tfidf >= CONTENT_SIMILARITY_THRESHOLD
        and not new_facts(new, old).numbers
        and not _types_conflict(new, old)
    ):
        return PairVerdict(
            True, "corroborated_overlap",
            f"Both articles have full text, report the same subject and overlap in wording, and the "
            f"headline carries no new figure (semantic {semantic:.2f}, text overlap {tfidf:.2f}).",
        )
    return PairVerdict(False, d.rule, d.reason)


def _rank_score(story: DayStory, reference: datetime) -> float:
    return compute_total_score(
        published_at=story.published_at,
        source_name=story.source_name,
        ai_relevance_score=1.0,
        duplicate_count=0,
        now=reference,
        window_hours=settings.news_window_hours,
    )[0]


def _later(a: DayStory, b: DayStory) -> bool:
    """True when `a` was published after `b` (ties broken by id), so the pair is always judged in
    chronological orientation: the later story as "new", the earlier as "old"."""
    return (a.published_at, a.story_id) > (b.published_at, b.story_id)


def _within_window(a: DayStory, b: DayStory) -> bool:
    if a.published_at is None or b.published_at is None:
        return False
    return abs((a.published_at - b.published_at).total_seconds()) / 3600.0 <= TIME_WINDOW_HOURS


def cluster_same_day(stories: list[DayStory], pinned: set[int], embedder: Embedder) -> DayResult:
    """Walk pinned stories first, then the rest oldest-first, against a pool of canonicals. Pinned
    stories (the day's episode selections) are never marked duplicate and are never displaced."""
    result = DayResult()
    if len(stories) < 2:
        return result

    ordered = [s for s in stories if s.story_id in pinned] + sorted(
        (s for s in stories if s.story_id not in pinned),
        key=lambda s: (s.published_at is None, s.published_at, s.story_id),
    )
    body = embedder.embed([s.features.embed_text for s in ordered])
    titles = embedder.embed([s.features.title for s in ordered])
    pos = {s.story_id: k for k, s in enumerate(ordered)}

    texts = [get_comparable_text(s.raw_content, s.raw_summary, s.title) for s in ordered]
    comparable = [k for k, t in enumerate(texts) if t is not None]
    tfidf = np.zeros((len(ordered), len(ordered)))
    if len(comparable) >= 2:
        sub = compute_pairwise_cosine_matrix([texts[k] for k in comparable])
        for a, ka in enumerate(comparable):
            for b, kb in enumerate(comparable):
                tfidf[ka, kb] = sub[a, b]

    dated = [s.published_at for s in ordered if s.published_at is not None]
    reference = max(dated) if dated else datetime.min
    pool: list[DayStory] = []

    for story in ordered:
        if story.story_id in pinned:
            pool.append(story)
            continue
        i = pos[story.story_id]
        best: tuple[DayStory, float, float, PairVerdict] | None = None
        for other in pool:
            if not _within_window(story, other):
                continue
            j = pos[other.story_id]
            result.comparisons += 1
            semantic = float(body[i] @ body[j])
            title_sim = float(titles[i] @ titles[j])
            same_source = bool(
                (story.features.url and story.features.url == other.features.url)
                or (story.features.content_hash and story.features.content_hash == other.features.content_hash)
            )
            if semantic < RELATED_FLOOR and not same_source:
                continue
            result.decide_calls += 1
            new, old = (story, other) if _later(story, other) else (other, story)
            verdict = judge_pair(new.features, old.features, semantic, title_sim, float(tfidf[i, j]))
            if verdict.duplicate and (best is None or semantic > best[1]):
                best = (other, semantic, float(tfidf[i, j]), verdict)

        if best is None:
            pool.append(story)
            continue

        other, semantic, lexical, verdict = best
        text_wins = verdict.rule == BARE_HEADLINE_RULE and story.features.article.quality != "title_only"
        if other.story_id not in pinned and (
            text_wins or _rank_score(story, reference) - _rank_score(other, reference) > ELECTION_MARGIN
        ):
            # The newer copy would rank higher: it becomes the canonical, so the event is not lost
            # when the older copy is cut by ranking. Everything that pointed at the old one follows.
            result.links[other.story_id] = Link(
                other.story_id, story.story_id, verdict.rule, verdict.reason, semantic, lexical
            )
            for link in result.links.values():
                if link.canonical_id == other.story_id and link.story_id != other.story_id:
                    link.canonical_id = story.story_id
            result.demoted.add(other.story_id)
            pool.remove(other)
            pool.append(story)
        else:
            result.links[story.story_id] = Link(
                story.story_id, other.story_id, verdict.rule, verdict.reason, semantic, lexical
            )

    return result
