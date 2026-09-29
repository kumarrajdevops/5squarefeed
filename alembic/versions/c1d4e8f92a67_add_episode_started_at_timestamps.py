"""add episode video_started_at and publish_started_at timestamps

Revision ID: c1d4e8f92a67
Revises: a3f6c92e1d47
Create Date: 2026-09-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# ---------------------------------------------------------
# Migration identifiers
# ---------------------------------------------------------

revision: str = "c1d4e8f92a67"
down_revision: Union[str, Sequence[str], None] = "a3f6c92e1d47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------
# Upgrade
# ---------------------------------------------------------

def upgrade() -> None:
    op.add_column(
        "episodes",
        sa.Column("video_started_at", sa.DateTime(timezone=True), nullable=True),
        schema="editorial",
    )
    op.add_column(
        "episodes",
        sa.Column("publish_started_at", sa.DateTime(timezone=True), nullable=True),
        schema="editorial",
    )


# ---------------------------------------------------------
# Downgrade
# ---------------------------------------------------------

def downgrade() -> None:
    op.drop_column("episodes", "publish_started_at", schema="editorial")
    op.drop_column("episodes", "video_started_at", schema="editorial")
