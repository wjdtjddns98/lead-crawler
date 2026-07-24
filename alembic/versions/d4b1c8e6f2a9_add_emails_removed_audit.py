"""add emails_removed column to review_audit (실존하지 않는 이메일 삭제 이력)

확정/거부 요청의 ``remove_emails`` 로 연락처를 지울 때 '무엇을 지웠는지'를 감사 이력에
남기기 위한 컬럼(JSON 배열 문자열). additive·NULL 허용이라 기존 데이터에 무손실 적용된다
(NULL = 이메일 삭제 없는 처리). 타입은 form_before/after 와 동일한 Text(절단 방지).

Revision ID: d4b1c8e6f2a9
Revises: c8e2f5a1d7b3
Create Date: 2026-07-24 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4b1c8e6f2a9"
down_revision: str | None = "c8e2f5a1d7b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_audit", sa.Column("emails_removed", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_audit", "emails_removed")
