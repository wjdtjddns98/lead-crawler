"""review_queue.note — 검수자 기타 메모(문의폼 미발송 사유 등, 엑셀 L 컬럼)

Revision ID: c8e2f5a1d7b3
Revises: b7d4e2a9c1f6
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c8e2f5a1d7b3"
down_revision: str | None = "b7d4e2a9c1f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_queue", sa.Column("note", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("review_queue", "note")
