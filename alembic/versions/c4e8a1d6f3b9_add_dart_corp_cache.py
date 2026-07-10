"""add dart_corp_cache — DART company.json 응답 캐시(corp 당 평생 1콜)

Revision ID: c4e8a1d6f3b9
Revises: b6d3f1a9c2e7
Create Date: 2026-07-10 12:40:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8a1d6f3b9"
down_revision: str | None = "b6d3f1a9c2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dart_corp_cache",
        sa.Column("corp_code", sa.String(length=8), primary_key=True),
        sa.Column("corp_name", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("induty_code", sa.String(length=16), nullable=True),
        sa.Column("info", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_dart_corp_cache_induty_code", "dart_corp_cache", ["induty_code"])


def downgrade() -> None:
    op.drop_index("ix_dart_corp_cache_induty_code", table_name="dart_corp_cache")
    op.drop_table("dart_corp_cache")
