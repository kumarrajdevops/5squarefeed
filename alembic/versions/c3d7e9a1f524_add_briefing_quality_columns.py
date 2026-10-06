"""source sufficiency and script quality columns

Revision ID: c3d7e9a1f524
Revises: b9e2f6a1c845
Create Date: 2026-10-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d7e9a1f524"
down_revision: Union[str, Sequence[str], None] = "b9e2f6a1c845"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STORIES = [
    ("content_extraction_method", sa.String(30)),
    ("source_word_count", sa.Integer()),
    ("source_sufficiency", sa.String(20)),
    ("sufficiency_detail", sa.Text()),
]
CONTENT = [
    ("script_word_count", sa.Integer()),
    ("script_sentence_count", sa.Integer()),
    ("script_quality_status", sa.String(20)),
    ("script_generation_reason", sa.Text()),
    ("script_meta", sa.Text()),
]


def upgrade() -> None:
    for name, type_ in STORIES:
        op.add_column("stories", sa.Column(name, type_, nullable=True), schema="editorial")
    for name, type_ in CONTENT:
        op.add_column("story_content", sa.Column(name, type_, nullable=True), schema="editorial")


def downgrade() -> None:
    for name, _ in reversed(CONTENT):
        op.drop_column("story_content", name, schema="editorial")
    for name, _ in reversed(STORIES):
        op.drop_column("stories", name, schema="editorial")
