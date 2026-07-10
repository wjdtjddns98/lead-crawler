"""add nps_workplace — 국민연금 가입 사업장 월간 스냅샷(업종·규모 우선 KR 발견 소재)

Revision ID: d7f2b9c4e1a8
Revises: c4e8a1d6f3b9
Create Date: 2026-07-10 14:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7f2b9c4e1a8"
down_revision: str | None = "c4e8a1d6f3b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nps_workplace",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("data_ym", sa.String(length=6), nullable=False, server_default=""),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("bizno_prefix", sa.String(length=6), nullable=True),
        sa.Column("address", sa.String(length=512), nullable=True),
        sa.Column("industry_code", sa.String(length=8), nullable=True),
        sa.Column("industry_name", sa.String(length=256), nullable=True),
        sa.Column("subscribers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notice_amt", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status_cd", sa.String(length=8), nullable=True),
        sa.Column("resigned_at", sa.String(length=8), nullable=True),
        sa.Column("pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_nps_workplace_industry_code", "nps_workplace", ["industry_code"])


def downgrade() -> None:
    op.drop_index("ix_nps_workplace_industry_code", table_name="nps_workplace")
    op.drop_table("nps_workplace")
