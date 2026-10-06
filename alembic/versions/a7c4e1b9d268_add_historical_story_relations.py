"""historical story relations (semantic duplicate detector)

Revision ID: a7c4e1b9d268
Revises: d5b8e2c4f917
Create Date: 2026-10-06

New table only. Existing stories, repeats_story_id/repeat_reason and canonical_story_id are
untouched.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c4e1b9d268"
down_revision: Union[str, Sequence[str], None] = "d5b8e2c4f917"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "historical_story_relations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("editorial.stories.id"), nullable=False),
        sa.Column(
            "matched_story_id", sa.Integer(), sa.ForeignKey("editorial.stories.id"), nullable=False
        ),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("semantic_similarity", sa.Float(), nullable=False),
        sa.Column("title_similarity", sa.Float(), nullable=True),
        sa.Column("development_match", sa.String(120), nullable=False),
        sa.Column("new_facts_detected", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rule", sa.String(40), nullable=False),
        sa.Column("content_basis", sa.String(20), nullable=False),
        sa.Column("method_version", sa.String(30), nullable=False),
        sa.Column("editor_override", sa.String(20), nullable=True),
        sa.Column("override_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "story_id", "matched_story_id", "method_version", name="uq_historical_relation"
        ),
        schema="editorial",
    )
    op.create_index(
        "ix_editorial_historical_story_relations_story_id",
        "historical_story_relations", ["story_id"], schema="editorial",
    )
    op.create_index(
        "ix_editorial_historical_story_relations_matched_story_id",
        "historical_story_relations", ["matched_story_id"], schema="editorial",
    )


def downgrade() -> None:
    op.drop_table("historical_story_relations", schema="editorial")
