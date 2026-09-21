"""add story taxonomy_category

Revision ID: e5a8c3f716d4
Revises: d3e7b4a2c951
Create Date: 2026-09-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# ---------------------------------------------------------
# Migration identifiers
# ---------------------------------------------------------

revision: str = "e5a8c3f716d4"
down_revision: Union[str, Sequence[str], None] = "d3e7b4a2c951"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------
# Upgrade
# ---------------------------------------------------------

def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column("taxonomy_category", sa.String(length=50), nullable=True),
    )


# ---------------------------------------------------------
# Downgrade
# ---------------------------------------------------------

def downgrade() -> None:
    op.drop_column("stories", "taxonomy_category")
