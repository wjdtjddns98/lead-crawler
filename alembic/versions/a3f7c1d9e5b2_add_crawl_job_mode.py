"""add mode/rounds_done to crawl_job (연속 크롤 모드)

웹 크롤 잡의 실행 모드 — once(단발 1회전) | continuous(취소까지 라운드 반복).
rounds_done 은 continuous 진행 가시성(완료 라운드 수). additive·server_default 라
기존 행(전부 단발)에 무손실 적용된다.

Revision ID: a3f7c1d9e5b2
Revises: e2b8d4f7a1c6
Create Date: 2026-07-02 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f7c1d9e5b2"
down_revision: str | None = "e2b8d4f7a1c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crawl_job",
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="once"),
    )
    op.add_column(
        "crawl_job",
        sa.Column("rounds_done", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("crawl_job", "rounds_done")
    op.drop_column("crawl_job", "mode")
