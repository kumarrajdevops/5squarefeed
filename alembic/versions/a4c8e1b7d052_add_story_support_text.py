"""add story_content.support_text (editable source/quote line)

Revision ID: a4c8e1b7d052
Revises: e7b1c4d9a203
Create Date: 2026-10-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4c8e1b7d052"
down_revision: Union[str, Sequence[str], None] = "e7b1c4d9a203"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("story_content", sa.Column("support_text", sa.Text(), nullable=True), schema="editorial")


def downgrade() -> None:
    op.drop_column("story_content", "support_text", schema="editorial")
