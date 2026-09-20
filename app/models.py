from datetime import date, datetime, timezone

from sqlalchemy import (
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


class Story(Base):
    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50),nullable=False,default="rss")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    author: Mapped[str | None] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(500))
    raw_summary: Mapped[str | None] = mapped_column(Text)

    # Full extracted article body (see app/content/article_extractor.py
    # + app/tasks/content_dedup.py) -- NULL until a fetch is attempted
    # (see content_fetch_status below), and stays NULL if extraction
    # fails; comparisons fall back to raw_summary/title in that case.
    raw_content: Mapped[str | None] = mapped_column(Text)

    # SHA-256 hex digest of normalized raw_content -- a cheap exact-copy
    # fast path (e.g. syndicated wire content) before ever running the
    # more expensive TF-IDF similarity comparison. Only set on a
    # successful fetch (see content_fetch_status, which is the actual
    # "was a fetch already attempted" idempotency flag -- content_hash
    # alone can't serve that role since a failed fetch has no content
    # to hash).
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(50),
        default="collected",
        nullable=False,
    )

    ai_relevance: Mapped[str] = mapped_column(
        String(50),
        default="pending",
        nullable=False,
    )

    ai_relevance_score: Mapped[float | None] = mapped_column(
        nullable=True,
    )

    filter_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # -----------------------------------------------------------
    # Deduplication
    # -----------------------------------------------------------
    # NULL = this story is canonical (unique, or the representative
    # of a duplicate group). Non-null = this story is a duplicate of
    # the story with that id. We never delete duplicate rows; the
    # raw article stays in the table for audit purposes, it's just
    # excluded from downstream ranking/publishing via this pointer.
    canonical_story_id: Mapped[int | None] = mapped_column(
        ForeignKey("stories.id"),
        nullable=True,
        index=True,
    )

    # Explanation of why this story was linked to its canonical
    # story (similarity scores, time delta) -- same audit pattern
    # as filter_reason on the AI relevance filter.
    dedup_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # -----------------------------------------------------------
    # Historical repeat detection (see app/tasks/content_dedup.py) --
    # a different question from canonical_story_id above.
    # canonical_story_id groups same-batch duplicates (two stories
    # ingested around the same time about the same event);
    # repeats_story_id flags that THIS story covers the same event as
    # something already narrated as primary in a PAST episode, even
    # under a different headline/outlet/URL -- content-similarity
    # based, not identity based (see the "never re-select" identity
    # check in app/tasks/ranking.py, which this complements). Soft
    # signal only, same as verification_status below -- does NOT
    # exclude from ranking eligibility (real false-positive risk found
    # during live verification at the current, still-unvalidated
    # similarity threshold; see TODO.md). Surfaced in the dashboard so
    # a human can decide.
    # -----------------------------------------------------------
    repeats_story_id: Mapped[int | None] = mapped_column(
        ForeignKey("stories.id"),
        nullable=True,
        index=True,
    )

    repeat_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Observability for the (genuinely fragile) full-article-text
    # fetch that powers content-based dedup/repeat-detection above.
    # NULL = never attempted. "success" | "empty_extraction" |
    # "fetch_error" | "non_html". A blocked/paywalled/JS-rendered site
    # degrades to a status here, never fought (no headless browser, no
    # CAPTCHA-solving) -- same standing rule as app/sources/
    # article_fetcher.py.
    content_fetch_status: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    # -----------------------------------------------------------
    # Fact Extraction + Verification Engine (see app/extraction/
    # fact_extractor.py, app/verification/engine.py, app/tasks/
    # verification.py). Soft signal only -- never gates ranking
    # eligibility, see verify_story()'s docstring for why.
    # -----------------------------------------------------------

    # JSON-serialized dict from extract_facts() (companies, products,
    # events, dates, claims) -- plain-text audit trail, same pattern as
    # filter_reason/dedup_reason/rank_reason.
    extracted_facts: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # pending -> verified/unverified. "pending" (never processed by
    # run_fact_extraction_and_verification yet) is treated as "no
    # bonus, not flagged either way" wherever this is consumed.
    verification_status: Mapped[str] = mapped_column(
        String(50),
        default="pending",
        nullable=False,
    )

    verification_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint("url", name="uq_stories_url"),
    )


class Episode(Base):
    """
    One row per ranking/selection run -- conceptually, one row per
    daily video episode. The actual Top-25 + 5-backup selection for
    this episode lives in EpisodeStory rows, not here, so this table
    stays small and simple.
    """

    __tablename__ = "episodes"

    id: Mapped[int] = mapped_column(primary_key=True)

    # The calendar day this episode is for (IST daily cycle, per the
    # architecture's 6 AM IST publication schedule). Not unique --
    # multiple selection runs on the same day are allowed and each
    # preserved as its own history entry.
    run_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # draft -> (future: approved -> published), managed by the
    # Editorial Dashboard phase, not this one.
    status: Mapped[str] = mapped_column(
        String(50),
        default="draft",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Final combined episode video -- all primary stories' individual
    # videos concatenated in rank order. pending -> producing -> ready
    # (or failed). See app/tasks/episode_video.py.
    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    video_status: Mapped[str] = mapped_column(
        String(50),
        default="pending",
        nullable=False,
    )

    # Automated Video QA (see app/qa/video_qa.py). pending -> passed/
    # failed. qa_report is a JSON-serialized list of individual check
    # results, stored as text (same pattern as the other audit-trail
    # reason fields elsewhere in this schema).
    qa_status: Mapped[str] = mapped_column(
        String(50),
        default="pending",
        nullable=False,
    )

    qa_report: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set when produce_episode_video last successfully completed /
    # when run_episode_qa last completed -- compared by the dashboard
    # to flag a QA result as stale (video was reproduced since QA last
    # ran). See app/tasks/episode_video.py and app/tasks/episode_qa.py.
    video_produced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    qa_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set whenever a reorder, swap, or a contained story's script edit
    # changes what this episode actually contains -- separate from
    # video_produced_at (Produce didn't necessarily run again yet).
    # Compared against qa_run_at the same way video_produced_at is, so
    # the dashboard flags QA as stale after these actions too, not just
    # after a re-Produce. See app/main.py's reorder/swap/
    # update_story_content endpoints.
    content_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Publishing Worker (YouTube, see app/publishing/youtube_publisher.py
    # and app/tasks/publishing.py). not_published -> publishing ->
    # published (or failed). Only reachable once status == "approved"
    # -- enforced by POST /episodes/{id}/publish, not by this column.
    publish_status: Mapped[str] = mapped_column(
        String(50),
        default="not_published",
        nullable=False,
    )

    youtube_video_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    youtube_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    publish_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EpisodeStory(Base):
    """
    One row per (episode, story) selection -- the actual Top-25 +
    5-backup list for a given episode. A story can appear in more
    than one episode across separate runs; each run's full snapshot
    is preserved independently rather than mutating Story itself.
    """

    __tablename__ = "episode_stories"

    id: Mapped[int] = mapped_column(primary_key=True)

    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id"),
        nullable=False,
        index=True,
    )

    story_id: Mapped[int] = mapped_column(
        ForeignKey("stories.id"),
        nullable=False,
        index=True,
    )

    # 1-30. 1-25 = primary (publish), 26-30 = backup.
    rank_position: Mapped[int] = mapped_column(Integer, nullable=False)

    # "primary" or "backup".
    selection_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    rank_score: Mapped[float] = mapped_column(Float, nullable=False)

    # Breakdown of the score components, for audit/explainability --
    # same pattern as Story.filter_reason and Story.dedup_reason.
    rank_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("episode_id", "story_id", name="uq_episode_story"),
        UniqueConstraint(
            "episode_id", "rank_position", name="uq_episode_rank_position"
        ),
    )


class StoryContent(Base):
    """
    Generated production artifacts for a single story -- script,
    narration audio, branded visual, and composed video. One row per
    story (1:1), produced by the app.tasks.content Celery chain.

    Scoped to individual stories rather than whole episodes for now:
    this is the first pass at the architecture's Script/Voice/
    Visual/Video stages, proven out end-to-end on one story at a time
    before being wired up to run across an entire Top-25 episode.
    """

    __tablename__ = "story_content"

    id: Mapped[int] = mapped_column(primary_key=True)

    story_id: Mapped[int] = mapped_column(
        ForeignKey("stories.id"),
        nullable=False,
        unique=True,
        index=True,
    )

    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Full spoken narration -- headline + summary, concatenated.
    # This is what gets fed to voice synthesis. Deliberately just the
    # facts: no "why it matters" editorializing or speculative
    # commentary.
    script_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_duration_seconds: Mapped[float | None] = mapped_column(nullable=True)

    # Real per-sentence timing reported by edge-tts during synthesis
    # (JSON list of {"text", "start", "end"} in seconds) -- captured at
    # the voice stage, consumed at the video stage to burn in
    # frame-accurate captions instead of a proportional character-count
    # estimate. See app/content/voice_generator.py.
    caption_segments: Mapped[str | None] = mapped_column(Text, nullable=True)

    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    captions_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # pending -> script_ready -> voice_ready -> visual_ready ->
    # video_ready (or failed, at any stage -- see error_message).
    status: Mapped[str] = mapped_column(
        String(50),
        default="pending",
        nullable=False,
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=True,
    )
