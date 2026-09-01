"""backfill_job 반복 옵션 — repeat_every_min(0=없음)·not_before(대기열 실행 가능 시각)

Revision ID: b3e7c9d2f4a1
Revises: a7c2e9f4d1b8
Create Date: 2026-09-01 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e7c9d2f4a1"
down_revision: str | None = "a7c2e9f4d1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 트랙 S 반복(웹 크롤실행 continuous 대체, 2026-09-01): done 시 같은 필터로 다음 잡을
    # not_before=now+repeat_every_min 으로 복제 적재. 0 이면 1회성(기존 동작).
    op.add_column(
        "backfill_job",
        sa.Column("repeat_every_min", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "backfill_job", sa.Column("not_before", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("backfill_job", "not_before")
    op.drop_column("backfill_job", "repeat_every_min")
