"""add ksic_industry_map — 업종코드→택소노미 통합 매핑(3층, 코드 단위 1회 분류)

Revision ID: e5c1f8a2d9b4
Revises: d7f2b9c4e1a8
Create Date: 2026-07-10 15:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5c1f8a2d9b4"
down_revision: str | None = "d7f2b9c4e1a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ksic_industry_map",
        sa.Column("industry_code", sa.String(length=8), primary_key=True),
        sa.Column("industry_name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("taxonomy_label", sa.String(length=128), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False, server_default=""),
        sa.Column(
            "mapped_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_ksic_industry_map_taxonomy_label", "ksic_industry_map", ["taxonomy_label"]
    )


def downgrade() -> None:
    op.drop_index("ix_ksic_industry_map_taxonomy_label", table_name="ksic_industry_map")
    op.drop_table("ksic_industry_map")
