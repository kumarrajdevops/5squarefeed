"""structured classification result and editor review decision

Revision ID: d5b8e2c4f917
Revises: c3d7e9a1f524
Create Date: 2026-10-06

Additive and nullable only: existing rows keep NULLs and are never reclassified.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5b8e2c4f917"
down_revision: Union[str, Sequence[str], None] = "c3d7e9a1f524"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = [
    ("classifier_version", sa.String(20)),
    ("classifier_disposition", sa.String(20)),
    ("classifier_ai_relatedness", sa.String(10)),
    ("classifier_content_flag", sa.String(10)),
    ("review_decision", sa.String(20)),
    ("reviewed_at", sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    for name, type_ in COLUMNS:
        op.add_column("stories", sa.Column(name, type_, nullable=True), schema="editorial")


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column("stories", name, schema="editorial")
