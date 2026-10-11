"""add raw.news_items.raw_summary_edit_count

Revision ID: eeb3a2335828
Revises: a7c4e1b9d268
Create Date: 2026-10-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# ---------------------------------------------------------
# Migration identifiers
# ---------------------------------------------------------

revision: str = "eeb3a2335828"
down_revision: Union[str, Sequence[str], None] = "a7c4e1b9d268"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------
# Upgrade
# ---------------------------------------------------------

def upgrade() -> None:
    op.add_column(
        "news_items",
        sa.Column("raw_summary_edit_count", sa.Integer(), nullable=False, server_default="0"),
        schema="raw",
    )
    op.alter_column("news_items", "raw_summary_edit_count", server_default=None, schema="raw")


# ---------------------------------------------------------
# Downgrade
# ---------------------------------------------------------

def downgrade() -> None:
    op.drop_column("news_items", "raw_summary_edit_count", schema="raw")
