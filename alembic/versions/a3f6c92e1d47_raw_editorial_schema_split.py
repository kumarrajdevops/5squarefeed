"""raw/editorial schema split -- calendar-day model, clean break

Revision ID: a3f6c92e1d47
Revises: b3e7f0a1c9d5
Create Date: 2026-09-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# ---------------------------------------------------------
# Migration identifiers
# ---------------------------------------------------------

revision: str = "a3f6c92e1d47"
down_revision: Union[str, Sequence[str], None] = "b3e7f0a1c9d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------
# Upgrade
# ---------------------------------------------------------


def upgrade() -> None:
    # Clean-break migration: the old rolling-22h-window ingestion model
    # and its single public.stories table are replaced entirely by a
    # calendar-day model (see app/dates.py) split across two schemas --
    # raw (pure collection, no editorial judgment) and editorial
    # (dedup/verification/ranking/episodes/production/QA/publishing).
    # Confirmed empty at the time this migration was written (the app's
    # own data was wiped as part of this same change) -- old tables are
    # dropped outright, not moved or preserved. See app/models.py for
    # the authoritative column-level docstrings this mirrors.

    op.execute("DROP TABLE IF EXISTS episode_stories CASCADE")
    op.execute("DROP TABLE IF EXISTS story_content CASCADE")
    op.execute("DROP TABLE IF EXISTS notifications CASCADE")
    op.execute("DROP TABLE IF EXISTS episodes CASCADE")
    op.execute("DROP TABLE IF EXISTS stories CASCADE")

    op.execute("CREATE SCHEMA IF NOT EXISTS raw")
    op.execute("CREATE SCHEMA IF NOT EXISTS editorial")

    # -------------------------------------------------
    # raw.news_items -- source-of-truth for collected source material.
    # Identity: (source_name, canonical_url) always unique; plus
    # (source_name, external_id) unique -- Postgres treats every NULL
    # as distinct, so this behaves as "unique when present" for
    # sources without a reliable external_id without needing a partial
    # index.
    # -------------------------------------------------

    op.create_table(
        "news_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False, server_default="rss"),
        sa.Column("external_id", sa.String(length=500), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "collected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("collection_date", sa.Date(), nullable=False),
        sa.Column("raw_summary", sa.Text(), nullable=True),
        sa.Column("raw_content", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="collected"),
        sa.UniqueConstraint("source_name", "canonical_url", name="uq_news_items_source_url"),
        sa.UniqueConstraint("source_name", "external_id", name="uq_news_items_source_external_id"),
        schema="raw",
    )
    op.create_index(
        "ix_raw_news_items_collection_date", "news_items", ["collection_date"], schema="raw"
    )

    # -------------------------------------------------
    # raw.collection_runs -- one row per complete "Collect News"
    # operation (RSS + Hacker News together, not one row per source).
    # -------------------------------------------------

    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("collection_date", sa.Date(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger_type", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("rss_items_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rss_items_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rss_items_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hn_items_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hn_items_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hn_items_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_details", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="raw",
    )
    op.create_index(
        "ix_raw_collection_runs_collection_date", "collection_runs", ["collection_date"], schema="raw"
    )

    # -------------------------------------------------
    # editorial.stories -- 1:1 companion to raw.news_items. id is both
    # this table's PK and its FK to raw.news_items.id (shared
    # identity, no separate surrogate key) -- never autoincrement,
    # always supplied explicitly as the matching NewsItem's id.
    # -------------------------------------------------

    op.create_table(
        "stories",
        sa.Column("id", sa.Integer(), sa.ForeignKey("raw.news_items.id"), primary_key=True, autoincrement=False),
        sa.Column("ai_relevance", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("ai_relevance_score", sa.Float(), nullable=True),
        sa.Column("filter_reason", sa.Text(), nullable=True),
        sa.Column("canonical_story_id", sa.Integer(), nullable=True),
        sa.Column("dedup_reason", sa.Text(), nullable=True),
        sa.Column("repeats_story_id", sa.Integer(), nullable=True),
        sa.Column("repeat_reason", sa.Text(), nullable=True),
        sa.Column("content_fetch_status", sa.String(length=20), nullable=True),
        sa.Column("extracted_facts", sa.Text(), nullable=True),
        sa.Column("verification_status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("verification_reason", sa.Text(), nullable=True),
        sa.Column("taxonomy_category", sa.String(length=50), nullable=True),
        schema="editorial",
    )
    # Self-referential FKs added after create_table -- both target this
    # same table, which doesn't exist yet at column-definition time.
    op.create_foreign_key(
        "fk_editorial_stories_canonical_story_id", "stories", "stories",
        ["canonical_story_id"], ["id"], source_schema="editorial", referent_schema="editorial",
    )
    op.create_foreign_key(
        "fk_editorial_stories_repeats_story_id", "stories", "stories",
        ["repeats_story_id"], ["id"], source_schema="editorial", referent_schema="editorial",
    )
    op.create_index(
        "ix_editorial_stories_canonical_story_id", "stories", ["canonical_story_id"], schema="editorial"
    )
    op.create_index(
        "ix_editorial_stories_repeats_story_id", "stories", ["repeats_story_id"], schema="editorial"
    )

    # -------------------------------------------------
    # editorial.episodes -- one row per coverage day (episode_date,
    # was run_date). uq_editorial_episodes_episode_date is the final
    # backstop behind ranking's own idempotency checks (see
    # app/tasks/ranking.py).
    # -------------------------------------------------

    op.create_table(
        "episodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("episode_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="draft"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("video_path", sa.Text(), nullable=True),
        sa.Column("video_status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("qa_status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("qa_report", sa.Text(), nullable=True),
        sa.Column("video_produced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("qa_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_status", sa.String(length=50), nullable=False, server_default="not_published"),
        sa.Column("youtube_video_id", sa.Text(), nullable=True),
        sa.Column("youtube_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("episode_date", name="uq_editorial_episodes_episode_date"),
        schema="editorial",
    )
    op.create_index(
        "ix_editorial_episodes_episode_date", "episodes", ["episode_date"], schema="editorial"
    )

    # -------------------------------------------------
    # editorial.episode_stories -- the Top-25 + 5-backup selection.
    # -------------------------------------------------

    op.create_table(
        "episode_stories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "episode_id", sa.Integer(), sa.ForeignKey("editorial.episodes.id"), nullable=False
        ),
        sa.Column(
            "story_id", sa.Integer(), sa.ForeignKey("editorial.stories.id"), nullable=False
        ),
        sa.Column("rank_position", sa.Integer(), nullable=False),
        sa.Column("selection_status", sa.String(length=20), nullable=False),
        sa.Column("rank_score", sa.Float(), nullable=False),
        sa.Column("rank_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("episode_id", "story_id", name="uq_episode_story"),
        sa.UniqueConstraint("episode_id", "rank_position", name="uq_episode_rank_position"),
        schema="editorial",
    )
    op.create_index(
        "ix_editorial_episode_stories_episode_id", "episode_stories", ["episode_id"], schema="editorial"
    )
    op.create_index(
        "ix_editorial_episode_stories_story_id", "episode_stories", ["story_id"], schema="editorial"
    )

    # -------------------------------------------------
    # editorial.story_content -- generated production artifacts.
    # -------------------------------------------------

    op.create_table(
        "story_content",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "story_id", sa.Integer(), sa.ForeignKey("editorial.stories.id"), nullable=False, unique=True
        ),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("script_text", sa.Text(), nullable=True),
        sa.Column("audio_path", sa.Text(), nullable=True),
        sa.Column("audio_duration_seconds", sa.Float(), nullable=True),
        sa.Column("caption_segments", sa.Text(), nullable=True),
        sa.Column("image_path", sa.Text(), nullable=True),
        sa.Column("captions_path", sa.Text(), nullable=True),
        sa.Column("video_path", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        schema="editorial",
    )
    op.create_index(
        "ix_editorial_story_content_story_id", "story_content", ["story_id"], schema="editorial"
    )

    # -------------------------------------------------
    # editorial.notifications -- failure-alert log.
    # -------------------------------------------------

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column(
            "episode_id", sa.Integer(), sa.ForeignKey("editorial.episodes.id"), nullable=True
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("delivered", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema="editorial",
    )
    op.create_index(
        "ix_editorial_notifications_episode_id", "notifications", ["episode_id"], schema="editorial"
    )


# ---------------------------------------------------------
# Downgrade
# ---------------------------------------------------------


def downgrade() -> None:
    # Deliberately one-way: this is a clean-break redesign (rolling
    # 22h-window ingestion -> calendar-day model; single public.stories
    # table -> raw/editorial schema split), not a reversible schema
    # tweak. There is no old-layout data to restore -- the tables this
    # migration drops were confirmed empty before being dropped. Roll
    # back by restoring a pre-migration database snapshot instead.
    raise NotImplementedError(
        "This migration is a deliberate one-way clean break -- see its module "
        "docstring. Restore a pre-migration database snapshot to roll back."
    )
