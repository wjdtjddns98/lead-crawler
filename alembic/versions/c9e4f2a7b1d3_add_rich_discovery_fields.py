"""add rich discovery fields to discovered_company (풍부필드 흡수 1단계)

등록처 응답이 이미 주는데 버려지던 값들의 슬롯: 주소 원문·정규화 지역·현지
등록번호(dedup 확정키)·티커·전화·IR URL·영문명. 전부 additive·NULL 허용이라
기존 데이터에 무손실 적용된다. region/reg_no 는 필터·조회 대상이라 인덱스.

Revision ID: c9e4f2a7b1d3
Revises: b4c7d1e8a2f9
Create Date: 2026-07-02 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e4f2a7b1d3"
down_revision: str | None = "b4c7d1e8a2f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("discovered_company", sa.Column("address", sa.Text(), nullable=True))
    op.add_column(
        "discovered_company", sa.Column("region", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "discovered_company", sa.Column("reg_no", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "discovered_company", sa.Column("ticker", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "discovered_company", sa.Column("phone", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "discovered_company", sa.Column("ir_url", sa.String(length=512), nullable=True)
    )
    op.add_column(
        "discovered_company", sa.Column("name_eng", sa.String(length=512), nullable=True)
    )
    op.create_index("ix_discovered_company_region", "discovered_company", ["region"])
    op.create_index("ix_discovered_company_reg_no", "discovered_company", ["reg_no"])


def downgrade() -> None:
    op.drop_index("ix_discovered_company_reg_no", table_name="discovered_company")
    op.drop_index("ix_discovered_company_region", table_name="discovered_company")
    op.drop_column("discovered_company", "name_eng")
    op.drop_column("discovered_company", "ir_url")
    op.drop_column("discovered_company", "phone")
    op.drop_column("discovered_company", "ticker")
    op.drop_column("discovered_company", "reg_no")
    op.drop_column("discovered_company", "region")
    op.drop_column("discovered_company", "address")
