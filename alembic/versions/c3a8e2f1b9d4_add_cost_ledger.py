"""add cost_ledger

Revision ID: c3a8e2f1b9d4
Revises: b2f4a1c9d3e7
Create Date: 2026-06-22 06:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a8e2f1b9d4"
down_revision: str | None = "b2f4a1c9d3e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 유료 외부 호출 과금 원장 — 월 예산(monthly_budget_krw) 추적.
    op.create_table(
        "cost_ledger",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("unit_cost_krw", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_krw", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("month_key", sa.String(length=7), nullable=False),
    )
    op.create_index("ix_cost_ledger_provider", "cost_ledger", ["provider"])
    op.create_index("ix_cost_ledger_month_key", "cost_ledger", ["month_key"])


def downgrade() -> None:
    op.drop_index("ix_cost_ledger_month_key", table_name="cost_ledger")
    op.drop_index("ix_cost_ledger_provider", table_name="cost_ledger")
    op.drop_table("cost_ledger")
