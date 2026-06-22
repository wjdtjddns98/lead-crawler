"""add review_queue.selected

Revision ID: e6f2b8c1a3d9
Revises: d5e1c7a4f2b8
Create Date: 2026-06-22 07:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f2b8c1a3d9"
down_revision: str | None = "d5e1c7a4f2b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 사람이 고른 최종 이메일 후보(candidates 중 1건). NULL=미선택(기본 대표 사용).
    op.add_column("review_queue", sa.Column("selected", sa.String(length=320), nullable=True))


def downgrade() -> None:
    op.drop_column("review_queue", "selected")
