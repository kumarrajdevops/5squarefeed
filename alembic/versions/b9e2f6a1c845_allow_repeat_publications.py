"""allow several publications per episode and environment

Revision ID: b9e2f6a1c845
Revises: a4c8e1b7d052
Create Date: 2026-10-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b9e2f6a1c845"
down_revision: Union[str, Sequence[str], None] = "a4c8e1b7d052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Every existing row was the only one for its (episode, environment), so 1.
    op.add_column(
        "episode_publications",
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="1"),
        schema="editorial",
    )
    op.drop_constraint("uq_episode_publications_episode_env", "episode_publications", schema="editorial", type_="unique")
    op.create_unique_constraint(
        "uq_episode_publications_episode_env_seq",
        "episode_publications",
        ["episode_id", "environment", "sequence"],
        schema="editorial",
    )


def downgrade() -> None:
    # Keeps only the latest publication per (episode, environment).
    op.execute(
        """
        DELETE FROM editorial.episode_publications p
        USING editorial.episode_publications q
        WHERE p.episode_id = q.episode_id AND p.environment = q.environment AND p.sequence < q.sequence
        """
    )
    op.drop_constraint("uq_episode_publications_episode_env_seq", "episode_publications", schema="editorial", type_="unique")
    op.create_unique_constraint(
        "uq_episode_publications_episode_env", "episode_publications", ["episode_id", "environment"], schema="editorial"
    )
    op.drop_column("episode_publications", "sequence", schema="editorial")
