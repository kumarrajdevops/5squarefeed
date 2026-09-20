"""add content similarity fields (repeats_story_id, repeat_reason, content_fetch_status)

Revision ID: b6d3f8c1a927
Revises: a4d7c1e69f28
Create Date: 2026-09-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# ---------------------------------------------------------
# Migration identifiers
# ---------------------------------------------------------

revision: str = "b6d3f8c1a927"
down_revision: Union[str, Sequence[str], None] = "a4d7c1e69f28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------
# Upgrade
# ---------------------------------------------------------

def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column(
            "repeats_story_id",
            sa.Integer(),
            sa.ForeignKey("stories.id"),
            nullable=True,
        ),
    )
    op.add_column("stories", sa.Column("repeat_reason", sa.Text(), nullable=True))
    op.add_column(
        "stories",
        sa.Column("content_fetch_status", sa.String(length=20), nullable=True),
    )
    op.create_index(
        "ix_stories_repeats_story_id", "stories", ["repeats_story_id"]
    )


# ---------------------------------------------------------
# Downgrade
# ---------------------------------------------------------

def downgrade() -> None:
    op.drop_index("ix_stories_repeats_story_id", table_name="stories")
    op.drop_column("stories", "content_fetch_status")
    op.drop_column("stories", "repeat_reason")
    op.drop_column("stories", "repeats_story_id")
