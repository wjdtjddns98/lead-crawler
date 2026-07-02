"""add market column to discovered_company (상장 시장 세분화)

listed 3값(listed/unlisted/unknown)의 세부 라벨 — DART corp_cls(KOSPI/KOSDAQ/KONEX)·
EDGAR 거래소(NASDAQ/NYSE/CBOE/OTC)·거래소 소스(PSE/SGX 등)가 기입한다.
additive·NULL 허용이라 기존 데이터에 무손실 적용된다.

Revision ID: e2b8d4f7a1c6
Revises: c9e4f2a7b1d3
Create Date: 2026-07-02 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b8d4f7a1c6"
down_revision: str | None = "c9e4f2a7b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discovered_company", sa.Column("market", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("discovered_company", "market")
