"""add homepage_before/homepage_after columns to review_audit (#185)

confirm 요청에서 홈페이지 수정값을 반영할 때 변경 전/후 값을 감사 이력에 남기기 위한
컬럼. additive·NULL 허용이라 기존 데이터에 무손실 적용된다(둘 다 NULL = 홈페이지
변경 없는 처리).

Revision ID: b6d3f1a9c2e7
Revises: c5d8e1f4a7b2
Create Date: 2026-07-08 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d3f1a9c2e7"
down_revision: str | None = "c5d8e1f4a7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_audit", sa.Column("homepage_before", sa.String(length=512), nullable=True)
    )
    op.add_column(
        "review_audit", sa.Column("homepage_after", sa.String(length=512), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("review_audit", "homepage_after")
    op.drop_column("review_audit", "homepage_before")
