"""add per-environment episode publications

Revision ID: e7b1c4d9a203
Revises: c1d4e8f92a67
Create Date: 2026-10-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7b1c4d9a203"
down_revision: Union[str, Sequence[str], None] = "c1d4e8f92a67"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "episode_publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("episode_id", sa.Integer(), sa.ForeignKey("editorial.episodes.id"), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="publishing"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("youtube_video_id", sa.Text(), nullable=True),
        sa.Column("youtube_url", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("episode_id", "environment", name="uq_episode_publications_episode_env"),
        schema="editorial",
    )
    op.create_index("ix_episode_publications_episode_id", "episode_publications", ["episode_id"], schema="editorial")

    # Episodes published before per-environment tracking existed were all
    # uploaded under dev (the only credentials this project has had).
    op.execute(
        """
        INSERT INTO editorial.episode_publications
            (episode_id, environment, status, started_at, published_at, youtube_video_id, youtube_url)
        SELECT id, 'dev', 'published', publish_started_at, published_at, youtube_video_id, youtube_url
        FROM editorial.episodes WHERE publish_status = 'published'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_episode_publications_episode_id", table_name="episode_publications", schema="editorial")
    op.drop_table("episode_publications", schema="editorial")
