"""backfill_job.industries — 포함식 업종 필터 스냅샷 (#372)

웹 백필 API 에 포함식 업종 필터(industries)를 배선하면서, 세대 교체·크래시 재기동
시에도 조건이 유실되지 않도록 job 행에 스냅샷 컬럼을 추가한다(_child_argv 는
DB 스냅샷에서만 argv 를 재구성하는 설계 계약 — exclude_industries 선례와 동형).
상수 기본값('') ADD COLUMN 이라 SQLite 도 일반 경로로 통과한다.

Revision ID: b7e4c1d9a3f6
Revises: a5c9e2d7b4f1
Create Date: 2026-08-21 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e4c1d9a3f6"
down_revision: str | None = "a5c9e2d7b4f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backfill_job",
        sa.Column("industries", sa.String(1024), nullable=False, server_default=sa.text("''")),
    )


def downgrade() -> None:
    op.drop_column("backfill_job", "industries")
