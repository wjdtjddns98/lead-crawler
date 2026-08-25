"""backfill_job 트랙 S(세그먼트 승격 큐) 필드 추가 (segment-jobs-design §2 PR2)

세그먼트(국가·업종·상장·지역) 지정 승격 요청을 backfill_job 위에 얹기 위한 스냅샷·진행
컬럼을 추가한다: listed/regions(대상 필터), priority(대기열 정렬), stage/discovered/
promote_cursor/failed_items(발견→승격 2단계 진행 자기보고). 전부 상수 기본값이라
SQLite 도 일반 add_column 경로로 통과한다(b7e4c1d9a3f6 선례).

Revision ID: d1f4a8c6e2b9
Revises: c3d8f1a6e2b7
Create Date: 2026-08-25 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f4a8c6e2b9"
down_revision: str | None = "c3d8f1a6e2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backfill_job",
        sa.Column("listed", sa.String(length=16), nullable=False, server_default=sa.text("'unknown'")),
    )
    op.add_column(
        "backfill_job",
        sa.Column("regions", sa.String(length=512), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "backfill_job",
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
    )
    op.add_column(
        "backfill_job",
        sa.Column("stage", sa.String(length=16), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "backfill_job",
        sa.Column("discovered", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "backfill_job",
        sa.Column("promote_cursor", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "backfill_job",
        sa.Column("failed_items", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("backfill_job", "failed_items")
    op.drop_column("backfill_job", "promote_cursor")
    op.drop_column("backfill_job", "discovered")
    op.drop_column("backfill_job", "stage")
    op.drop_column("backfill_job", "priority")
    op.drop_column("backfill_job", "regions")
    op.drop_column("backfill_job", "listed")
