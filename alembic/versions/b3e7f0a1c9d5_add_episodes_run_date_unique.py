"""add unique constraint on episodes.run_date

Revision ID: b3e7f0a1c9d5
Revises: e5a8c3f716d4
Create Date: 2026-09-22
"""

from typing import Sequence, Union

from alembic import op


# ---------------------------------------------------------
# Migration identifiers
# ---------------------------------------------------------

revision: str = "b3e7f0a1c9d5"
down_revision: Union[str, Sequence[str], None] = "e5a8c3f716d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------
# Upgrade
# ---------------------------------------------------------


def upgrade() -> None:
    # Enforces "one canonical Episode per run_date" at the database
    # layer -- the final backstop behind run_ranking_selection()'s own
    # explicit existing-episode check (app/tasks/ranking.py). This
    # will fail outright if any duplicate run_date rows still exist;
    # they were cleaned up manually (by explicit episode id, not a
    # broad DELETE WHERE run_date) before this migration was written --
    # see TODO.md for the cleanup record. The MVP-era rationale for
    # deliberately not having this constraint (preserved verbatim in
    # the original "add_episodes" migration) no longer applies now
    # that repeated selection is itself idempotent.
    op.create_unique_constraint("uq_episodes_run_date", "episodes", ["run_date"])


# ---------------------------------------------------------
# Downgrade
# ---------------------------------------------------------


def downgrade() -> None:
    op.drop_constraint("uq_episodes_run_date", "episodes", type_="unique")
