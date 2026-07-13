"""crawl_job 실행옵션 스냅샷(target_count/regions/discovery_only)

워치독 재기동이 원 요청 조건을 복원할 수 있도록 잡 실행에 영향 주는 옵션을 행에
저장한다(전수리뷰 HIGH: 미저장이면 재기동 시 지역한정 크롤이 전국으로 확대되고
target_count 상한이 사라지며 discovery_only 가 풀 enrich 로 바뀐다).
additive + server_default 라 기존 행에 무손실 적용(기존 행=기본값, 구 동작과 동일).

Revision ID: b7d4e2a9c1f6
Revises: a2e6c9d1f4b7
Create Date: 2026-07-13 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d4e2a9c1f6"
down_revision: str | None = "a2e6c9d1f4b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crawl_job",
        sa.Column("target_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "crawl_job",
        sa.Column("regions", sa.String(length=256), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "crawl_job",
        sa.Column(
            "discovery_only", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("crawl_job", "discovery_only")
    op.drop_column("crawl_job", "regions")
    op.drop_column("crawl_job", "target_count")
