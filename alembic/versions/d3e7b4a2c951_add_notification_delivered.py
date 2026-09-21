"""add notifications.delivered

Revision ID: d3e7b4a2c951
Revises: c8f1a5d92e63
Create Date: 2026-09-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# ---------------------------------------------------------
# Migration identifiers
# ---------------------------------------------------------

revision: str = "d3e7b4a2c951"
down_revision: Union[str, Sequence[str], None] = "c8f1a5d92e63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------
# Upgrade
# ---------------------------------------------------------

def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column(
            "delivered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


# ---------------------------------------------------------
# Downgrade
# ---------------------------------------------------------

def downgrade() -> None:
    op.drop_column("notifications", "delivered")
