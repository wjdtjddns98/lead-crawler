"""add review_queue.has_attachment / manager

Revision ID: c3d8f1a6e2b7
Revises: b7e4c1d9a3f6
Create Date: 2026-08-24 15:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d8f1a6e2b7"
down_revision: str | None = "b7e4c1d9a3f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 첨부파일 유무(검수자 체크) — NULL=미확인, True/False=사람이 확인한 값.
    op.add_column("review_queue", sa.Column("has_attachment", sa.Boolean(), nullable=True))
    # 상대 회사 담당자명(검수자 기입) — 엑셀 H(담당자) 컬럼으로 export 된다.
    op.add_column("review_queue", sa.Column("manager", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("review_queue", "manager")
    op.drop_column("review_queue", "has_attachment")
