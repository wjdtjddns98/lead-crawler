"""add form_before/form_after columns to review_audit (#241)

confirm 요청의 문의폼 유무(has_form) 교정값을 반영할 때 변경 전/후 폼 URL 을 감사
이력에 남기기 위한 컬럼. additive·NULL 허용이라 기존 데이터에 무손실 적용된다
(둘 다 NULL = 폼 변경 없는 처리). 타입은 ContactRow.value 와 동일한 Text(절단 방지).

Revision ID: a2e6c9d1f4b7
Revises: f8b3d6e1c4a7
Create Date: 2026-07-13 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2e6c9d1f4b7"
down_revision: str | None = "f8b3d6e1c4a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_audit", sa.Column("form_before", sa.Text(), nullable=True))
    op.add_column("review_audit", sa.Column("form_after", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_audit", "form_after")
    op.drop_column("review_audit", "form_before")
