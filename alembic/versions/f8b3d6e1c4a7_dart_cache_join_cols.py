"""dart_corp_cache 조인 컬럼 — bizno·name_norm(인덱스): NPS×DART 무쿼터 enrich 룩업

Revision ID: f8b3d6e1c4a7
Revises: e5c1f8a2d9b4
Create Date: 2026-07-10 15:50:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8b3d6e1c4a7"
down_revision: str | None = "e5c1f8a2d9b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dart_corp_cache", sa.Column("bizno", sa.String(length=10), nullable=True))
    op.add_column(
        "dart_corp_cache", sa.Column("name_norm", sa.String(length=255), nullable=True)
    )
    op.create_index("ix_dart_corp_cache_bizno", "dart_corp_cache", ["bizno"])
    op.create_index("ix_dart_corp_cache_name_norm", "dart_corp_cache", ["name_norm"])


def downgrade() -> None:
    op.drop_index("ix_dart_corp_cache_name_norm", table_name="dart_corp_cache")
    op.drop_index("ix_dart_corp_cache_bizno", table_name="dart_corp_cache")
    op.drop_column("dart_corp_cache", "name_norm")
    op.drop_column("dart_corp_cache", "bizno")
