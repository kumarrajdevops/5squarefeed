from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


# =================================================================
# raw schema -- source-of-truth for collected source material.
# Collect first, process later: nothing in this schema is ever an
# editorial judgment (no ai_relevance, no ranking, no dedup, no
# verification, no taxonomy). See app/tasks/ingestion.py /
# ingestion_hackernews.py, which write here and ONLY here.
# =================================================================


class NewsItem(Base):
    """
    One row per source item collected for a given collection_date
    (the IST calendar day this row was collected FOR -- see
    app/dates.py's target_collection_date(), not the day collection
    actually ran). Preserves everything the source exposed for that
    day; no Top-25/ranking/relevance filtering happens here.

    Identity/technical dedup: NOT url alone. Two unique indexes --
    (source_name, canonical_url) always, plus a partial
    (source_name, external_id) WHERE external_id IS NOT NULL for
    sources with a reliable id (Hacker News's objectID always has
    one; RSS's guid/id often does, not guaranteed). Repeated
    collection of the same item updates this row (collected_at,
    content fields) rather than creating a duplicate.
    """

    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("source_name", "canonical_url", name="uq_news_items_source_url"),
        UniqueConstraint(
            "source_name", "external_id", name="uq_news_items_source_external_id"
        ),
        {"schema": "raw"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, default="rss")

    # Reliable per-source id when the source provides one (HN's
    # objectID always does; RSS's guid/id sometimes does). Nullable --
    # the (source_name, external_id) unique index above is PARTIAL
    # (WHERE external_id IS NOT NULL), so multiple NULLs are allowed.
    external_id: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # The item's URL exactly as extracted (.strip()) -- no additional
    # normalization (tracking-param stripping, case-folding, etc.)
    # added; revisit only if near-duplicate URLs prove to be a real
    # problem in practice.
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String(255))

    # The source's own publish time -- distinct from collected_at
    # (when we retrieved it) and collection_date (which coverage day
    # this row belongs to). Never conflate these three.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # The target_date this row was collected for (app/dates.py's
    # target_collection_date()) -- the business partition for raw
    # collection. Indexed: this is the primary lookup processing uses.
    collection_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    raw_summary: Mapped[str | None] = mapped_column(Text)

    # Full extracted article body (see app/content/article_extractor.py)
    # -- NULL until a fetch is attempted (content_fetch_status lives on
    # editorial.StoryState, not here, since "was a fetch attempted for
    # dedup purposes" is an editorial-processing concern, not a raw
    # collection fact).
    raw_content: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(String(50), default="collected", nullable=False)


class CollectionRun(Base):
    """
    One row per complete "Collect News" operation (RSS + Hacker News
    together, not one row per source) -- the durable, queryable record
    of what a collection click/scheduled run actually did. Supersedes
    relying on ephemeral Celery task results for anything the
    dashboard needs to show after the fact.
    """

    __tablename__ = "collection_runs"
    __table_args__ = {"schema": "raw"}

    id: Mapped[int] = mapped_column(primary_key=True)

    collection_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # "manual" | "scheduled"
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")

    # "running" -> "success" | "failed"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")

    rss_items_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rss_items_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rss_items_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    hn_items_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hn_items_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hn_items_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


# =================================================================
# editorial schema -- deduplication, verification, categorization,
# ranking, episode assembly, production, QA, publishing. Reads from
# raw.news_items; never mutates or deletes it.
# =================================================================


class StoryState(Base):
    """
    1:1 editorial companion to a raw.NewsItem -- id is both this
    table's PK and its FK to raw.news_items.id (no separate surrogate
    key; the two rows share one identity). Created by the explicit
    processing operation (app/tasks/scheduled.py), never by
    collection -- raw ingestion writes zero rows here.
    """

    __tablename__ = "stories"
    __table_args__ = {"schema": "editorial"}

    id: Mapped[int] = mapped_column(
        ForeignKey("raw.news_items.id"), primary_key=True
    )

    ai_relevance: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    ai_relevance_score: Mapped[float | None] = mapped_column(nullable=True)
    filter_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # NULL = canonical (unique, or representative of a duplicate
    # group). Non-null = duplicate of the editorial.stories row with
    # that id. Rows are never deleted; just excluded downstream.
    canonical_story_id: Mapped[int | None] = mapped_column(
        ForeignKey("editorial.stories.id"), nullable=True, index=True
    )
    dedup_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Content-similarity historical-repeat detection -- a different
    # question from canonical_story_id (same-batch duplicates):
    # flags that this story covers an event already narrated as
    # primary in a PAST episode. Soft signal only, never excludes.
    repeats_story_id: Mapped[int | None] = mapped_column(
        ForeignKey("editorial.stories.id"), nullable=True, index=True
    )
    repeat_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # NULL = never attempted. "success" | "empty_extraction" |
    # "fetch_error" | "non_html". See app/content/article_extractor.py.
    content_fetch_status: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Fact Extraction + Verification Engine + taxonomy -- soft signals,
    # never gate ranking eligibility.
    extracted_facts: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str] = mapped_column(
        String(50), default="pending", nullable=False
    )
    verification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    taxonomy_category: Mapped[str | None] = mapped_column(String(50), nullable=True)


class Episode(Base):
    """
    One row per coverage day -- conceptually, one row per daily video
    episode. The actual Top-25 + 5-backup selection lives in
    EpisodeStory rows, not here.
    """

    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint("episode_date", name="uq_editorial_episodes_episode_date"),
        {"schema": "editorial"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # The calendar day this episode is FOR (app/dates.py's
    # target_collection_date()) -- the business identity. One Episode
    # per episode_date, enforced by uq_editorial_episodes_episode_date.
    # Publishing time never determines this; see published_at below,
    # which is a completely separate concept (when we actually
    # uploaded to YouTube).
    episode_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)

    qa_status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    qa_report: Mapped[str | None] = mapped_column(Text, nullable=True)

    video_produced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    qa_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    publish_status: Mapped[str] = mapped_column(
        String(50), default="not_published", nullable=False
    )
    youtube_video_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    youtube_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publish_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EpisodeStory(Base):
    """
    One row per (episode, story) selection -- the actual Top-25 +
    5-backup list for a given episode. story_id references
    editorial.stories.id (== the same raw.news_items.id).
    """

    __tablename__ = "episode_stories"
    __table_args__ = (
        UniqueConstraint("episode_id", "story_id", name="uq_episode_story"),
        UniqueConstraint("episode_id", "rank_position", name="uq_episode_rank_position"),
        {"schema": "editorial"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    episode_id: Mapped[int] = mapped_column(
        ForeignKey("editorial.episodes.id"), nullable=False, index=True
    )
    story_id: Mapped[int] = mapped_column(
        ForeignKey("editorial.stories.id"), nullable=False, index=True
    )

    rank_position: Mapped[int] = mapped_column(Integer, nullable=False)
    selection_status: Mapped[str] = mapped_column(String(20), nullable=False)
    rank_score: Mapped[float] = mapped_column(Float, nullable=False)
    rank_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class StoryContent(Base):
    """
    Generated production artifacts for a single story -- script,
    narration audio, branded visual, and composed video. One row per
    story (1:1), keyed to editorial.stories.id.
    """

    __tablename__ = "story_content"
    __table_args__ = {"schema": "editorial"}

    id: Mapped[int] = mapped_column(primary_key=True)

    story_id: Mapped[int] = mapped_column(
        ForeignKey("editorial.stories.id"), nullable=False, unique=True, index=True
    )

    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    script_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    caption_segments: Mapped[str | None] = mapped_column(Text, nullable=True)

    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    captions_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc), nullable=True
    )


class Notification(Base):
    """
    Failure-alert record. Scope deliberately narrow -- only the two
    failure classes that mean "the whole day's episode didn't happen."
    """

    __tablename__ = "notifications"
    __table_args__ = {"schema": "editorial"}

    id: Mapped[int] = mapped_column(primary_key=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    category: Mapped[str] = mapped_column(String(50), nullable=False)
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("editorial.episodes.id"), nullable=True, index=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
