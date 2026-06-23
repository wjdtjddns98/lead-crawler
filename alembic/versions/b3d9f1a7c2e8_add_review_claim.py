"""add review_queue.claimed_by/claimed_at (동시 처리 당겨가기 점유)

Revision ID: b3d9f1a7c2e8
Revises: a1c4e8b2f6d3
Create Date: 2026-06-23 12:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d9f1a7c2e8"
down_revision: str | None = "a1c4e8b2f6d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_queue", sa.Column("claimed_by", sa.String(length=32), nullable=True))
    op.add_column(
        "review_queue", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_review_queue_claimed_by", "review_queue", ["claimed_by"])
    op.create_foreign_key(
        "fk_review_queue_claimed_by_app_user",
        "review_queue",
        "app_user",
        ["claimed_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_review_queue_claimed_by_app_user", "review_queue", type_="foreignkey"
    )
    op.drop_index("ix_review_queue_claimed_by", table_name="review_queue")
    op.drop_column("review_queue", "claimed_at")
    op.drop_column("review_queue", "claimed_by")
