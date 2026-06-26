"""add dedup_candidate table (중복해소 C4 워크벤치)

사다리(C1)/LLM(C2)이 못 가른 경계 중복후보 쌍 + 사람 결정(merged/separated) 영속.
신규 테이블만 추가하는 additive 변경이라 기존 데이터에 무손실.

Revision ID: a7c3e1d9f2b5
Revises: e9b1d4c7a2f5
Create Date: 2026-06-26 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e1d9f2b5"
down_revision: str | None = "e9b1d4c7a2f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dedup_candidate",
        # sha1(정렬된 key_a + NUL + key_b) — 입력 순서 무관 멱등 upsert.
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("key_a", sa.String(length=255), nullable=False),
        sa.Column("key_b", sa.String(length=255), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("name_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("reason", sa.Text(), nullable=False, server_default=sa.text("''")),
        # C2 LLM 판정(opt-in) — 미판정이면 NULL.
        sa.Column("llm_same", sa.Boolean(), nullable=True),
        sa.Column("llm_confidence", sa.Float(), nullable=True),
        sa.Column("llm_reason", sa.Text(), nullable=True),
        sa.Column("llm_model", sa.String(length=64), nullable=True),
        # pending(미결) | merged(동일확정) | separated(분리).
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("survivor_key", sa.String(length=255), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_dedup_candidate_key_a", "dedup_candidate", ["key_a"])
    op.create_index("ix_dedup_candidate_key_b", "dedup_candidate", ["key_b"])
    op.create_index("ix_dedup_candidate_status", "dedup_candidate", ["status"])


def downgrade() -> None:
    op.drop_index("ix_dedup_candidate_status", table_name="dedup_candidate")
    op.drop_index("ix_dedup_candidate_key_b", table_name="dedup_candidate")
    op.drop_index("ix_dedup_candidate_key_a", table_name="dedup_candidate")
    op.drop_table("dedup_candidate")
