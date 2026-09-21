"""add notifications table

Revision ID: c8f1a5d92e63
Revises: b6d3f8c1a927
Create Date: 2026-09-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# ---------------------------------------------------------
# Migration identifiers
# ---------------------------------------------------------

revision: str = "c8f1a5d92e63"
down_revision: Union[str, Sequence[str], None] = "b6d3f8c1a927"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------
# Upgrade
# ---------------------------------------------------------

def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column(
            "episode_id",
            sa.Integer(),
            sa.ForeignKey("episodes.id"),
            nullable=True,
        ),
        sa.Column("message", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_notifications_episode_id", "notifications", ["episode_id"]
    )


# ---------------------------------------------------------
# Downgrade
# ---------------------------------------------------------

def downgrade() -> None:
    op.drop_index("ix_notifications_episode_id", table_name="notifications")
    op.drop_table("notifications")
