"""add editorial.stories.excluded_by_editor

Revision ID: 299f62f2ef5f
Revises: eeb3a2335828
Create Date: 2026-10-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# ---------------------------------------------------------
# Migration identifiers
# ---------------------------------------------------------

revision: str = "299f62f2ef5f"
down_revision: Union[str, Sequence[str], None] = "eeb3a2335828"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------
# Upgrade
# ---------------------------------------------------------

def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column("excluded_by_editor", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema="editorial",
    )
    op.alter_column("stories", "excluded_by_editor", server_default=None, schema="editorial")


# ---------------------------------------------------------
# Downgrade
# ---------------------------------------------------------

def downgrade() -> None:
    op.drop_column("stories", "excluded_by_editor", schema="editorial")
