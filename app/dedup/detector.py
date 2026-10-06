"""
Corpus loading, candidate retrieval and verdict selection for the historical duplicate detector.

Corpus: stories that were primary in an approved or published episode (what 5squareFeed actually
reported to its audience) -- never raw stories, backups, rejected stories or unreviewed drafts.
Optionally (replay sensitivity only) draft episodes' primaries too.

Retrieval is a funnel, not a scan of pairs: one vectorised dot product of a candidate against the
corpus matrix (a few hundred rows x 384 floats), then at most MAX_CANDIDATES above the relatedness
floor go through the full rule-based comparison, ranked so that candidates sharing a specific
headline entity come first.
"""
import time
from dataclasses import dataclass, field
from datetime import date

import numpy as np
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.dedup.decision import RELATED_FLOOR, Decision, decide
from app.dedup.embedder import Embedder
from app.dedup.features import StoryFeatures, build_features
from app.models import Episode, EpisodeStory, NewsItem, StoryState

MAX_CANDIDATES = 10
_ID_CHUNK = 500


def _chunks(ids: list[int]):
    for i in range(0, len(ids), _ID_CHUNK):
        yield ids[i : i + _ID_CHUNK]


def load_features(db: Session, story_ids: list[int]) -> dict[int, StoryFeatures]:
    out: dict[int, StoryFeatures] = {}
    for chunk in _chunks(sorted(set(story_ids))):
        rows = (
            db.query(NewsItem, StoryState)
            .join(StoryState, StoryState.id == NewsItem.id)
            .filter(NewsItem.id.in_(chunk))
            .all()
        )
        for item, state in rows:
            out[item.id] = build_features(
                item.id,
                item.title,
                item.raw_content,
                item.raw_summary,
                state.content_fetch_status,
                item.canonical_url,
                item.content_hash,
                item.source_name,
            )
    return out


def published_primary_rows(db: Session, include_drafts: bool = False) -> list[tuple[int, date]]:
    """(story_id, episode_date) for every primary selection in an approved/published episode."""
    query = (
        db.query(EpisodeStory.story_id, Episode.episode_date)
        .join(Episode, Episode.id == EpisodeStory.episode_id)
        .filter(EpisodeStory.selection_status == "primary")
    )
    if not include_drafts:
        query = query.filter(or_(Episode.status == "approved", Episode.publish_status == "published"))
    first_seen: dict[int, date] = {}
    for story_id, episode_date in query.all():
        if story_id not in first_seen or episode_date < first_seen[story_id]:
            first_seen[story_id] = episode_date
    return sorted(first_seen.items(), key=lambda row: (row[1], row[0]))


@dataclass
class Stats:
    stories_processed: int = 0
    corpus_size: int = 0
    semantic_comparisons: int = 0   # candidate x corpus vector comparisons
    candidate_comparisons: int = 0  # full rule-based comparisons (decide() calls)
    runtime_seconds: float = 0.0
    embed_seconds: float = 0.0
    slowest: list[tuple[float, int]] = field(default_factory=list)  # (seconds, story_id)

    def note_time(self, story_id: int, seconds: float) -> None:
        self.slowest.append((seconds, story_id))
        self.slowest = sorted(self.slowest, reverse=True)[:5]

    @property
    def average_seconds(self) -> float:
        return self.runtime_seconds / self.stories_processed if self.stories_processed else 0.0


@dataclass
class Verdict:
    story_id: int
    matched_story_id: int
    decision: Decision
    similarity: float
    title_similarity: float

    @property
    def is_duplicate(self) -> bool:
        return self.decision.is_duplicate


class HistoricalCorpus:
    def __init__(self, features: dict[int, StoryFeatures], dates: dict[int, date], embedder: Embedder):
        self.ids = [sid for sid in dates if sid in features]
        self.features = {sid: features[sid] for sid in self.ids}
        self.dates = np.array([dates[sid] for sid in self.ids], dtype="datetime64[D]")
        started = time.perf_counter()
        if self.ids:
            self.body = embedder.embed([self.features[s].embed_text for s in self.ids])
            self.title = embedder.embed([self.features[s].title for s in self.ids])
        else:
            self.body = self.title = np.zeros((0, 1), dtype=np.float32)
        self.embed_seconds = time.perf_counter() - started
        self.pos = {sid: i for i, sid in enumerate(self.ids)}
        self.by_url = {f.url: s for s, f in self.features.items() if f.url}
        self.by_hash = {f.content_hash: s for s, f in self.features.items() if f.content_hash}

    def __len__(self) -> int:
        return len(self.ids)

    def visible_mask(self, before: date | None) -> np.ndarray:
        if before is None:
            return np.ones(len(self.ids), dtype=bool)
        return self.dates < np.datetime64(before)

    def detect(
        self,
        story: StoryFeatures,
        body_vec: np.ndarray,
        title_vec: np.ndarray,
        before: date | None,
        stats: Stats | None = None,
    ) -> Verdict | None:
        """Best verdict for one story against the stories published before `before`, or None when
        nothing published relates to it."""
        started = time.perf_counter()
        mask = self.visible_mask(before)
        visible = np.flatnonzero(mask)
        if visible.size == 0:
            return None

        sims = self.body[visible] @ body_vec
        title_sims = self.title[visible] @ title_vec
        if stats:
            stats.semantic_comparisons += int(visible.size)

        picked: dict[int, float] = {}
        for url_hash_index, key in ((self.by_url, story.url), (self.by_hash, story.content_hash)):
            sid = url_hash_index.get(key) if key else None
            if sid is not None and mask[self.pos[sid]]:
                picked[self.pos[sid]] = 2.0  # exact source match always compared
        entities = story.specific_title_entities
        for pos in np.flatnonzero(sims >= RELATED_FLOOR):
            idx = int(visible[pos])
            shares = bool(entities & self.features[self.ids[idx]].specific_title_entities)
            picked.setdefault(idx, float(sims[pos]) + (1.0 if shares else 0.0))
        order = sorted(picked, key=lambda i: picked[i], reverse=True)[:MAX_CANDIDATES]

        position = {int(v): n for n, v in enumerate(visible)}
        results: list[Verdict] = []
        for idx in order:
            n = position[idx]
            decision = decide(story, self.features[self.ids[idx]], float(sims[n]), float(title_sims[n]))
            if stats:
                stats.candidate_comparisons += 1
            if decision.related:
                results.append(
                    Verdict(story.story_id, self.ids[idx], decision, float(sims[n]), float(title_sims[n]))
                )

        if stats:
            stats.note_time(story.story_id, time.perf_counter() - started)
        if not results:
            return None
        duplicates = [v for v in results if v.is_duplicate]
        pool = duplicates or results
        return max(pool, key=lambda v: v.similarity)


def build_corpus(
    db: Session, embedder: Embedder, include_drafts: bool = False
) -> HistoricalCorpus:
    rows = published_primary_rows(db, include_drafts)
    features = load_features(db, [sid for sid, _ in rows])
    return HistoricalCorpus(features, dict(rows), embedder)


def detect_stories(
    db: Session,
    story_ids: list[int],
    corpus: HistoricalCorpus,
    embedder: Embedder,
    before_for: dict[int, date],
    stats: Stats | None = None,
) -> dict[int, Verdict | None]:
    """Verdict (or None) per story. `before_for` gives each story's as-of date: only episodes
    dated strictly earlier count as already published."""
    started = time.perf_counter()
    features = load_features(db, story_ids)
    ids = [sid for sid in story_ids if sid in features]
    embed_started = time.perf_counter()
    body = embedder.embed([features[s].embed_text for s in ids]) if ids else []
    title = embedder.embed([features[s].title for s in ids]) if ids else []
    if stats:
        stats.embed_seconds += time.perf_counter() - embed_started

    out: dict[int, Verdict | None] = {}
    for n, sid in enumerate(ids):
        out[sid] = corpus.detect(features[sid], body[n], title[n], before_for[sid], stats)
        if stats:
            stats.stories_processed += 1
    if stats:
        stats.corpus_size = len(corpus)
        stats.runtime_seconds += time.perf_counter() - started
    return out
