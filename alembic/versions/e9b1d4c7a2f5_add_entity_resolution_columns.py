"""add entity-resolution columns to discovered_company (중복해소 C0)

canonical_name + duplicate_of(자기참조) + 머지 감사(merged_at/by/reason) 추가.
전부 additive·NULL 허용이라 기존 20,179건 데이터에 무손실 적용된다.

Revision ID: e9b1d4c7a2f5
Revises: d2e5f8a1c6b9
Create Date: 2026-06-25 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9b1d4c7a2f5"
down_revision: str | None = "d2e5f8a1c6b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 캐노니컬(법인 정식) 회사명 라벨 — survivorship 결과(C3). NULL=미라벨.
    op.add_column(
        "discovered_company", sa.Column("canonical_name", sa.String(length=512), nullable=True)
    )
    # 이 행이 중복으로 판정돼 흡수된 생존 레코드의 canonical_key(자기참조). NULL=생존/미판정.
    op.add_column(
        "discovered_company", sa.Column("duplicate_of", sa.String(length=255), nullable=True)
    )
    # 머지 감사 — 언제·누가/무엇이·어떤 근거로(가역 추적).
    op.add_column(
        "discovered_company", sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "discovered_company", sa.Column("merged_by", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "discovered_company", sa.Column("merge_reason", sa.String(length=255), nullable=True)
    )
    op.create_index(
        "ix_discovered_company_duplicate_of", "discovered_company", ["duplicate_of"]
    )
    # 자기참조 FK(생존 레코드 삭제 시 SET NULL — 중복표시는 사라져도 행은 보존, 제약②).
    # SQLite 는 사후 ALTER 로 FK 를 못 붙이므로 건너뛴다(create_all 경로엔 이미 정의됨).
    if op.get_bind().dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_discovered_company_duplicate_of",
            "discovered_company",
            "discovered_company",
            ["duplicate_of"],
            ["canonical_key"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint(
            "fk_discovered_company_duplicate_of", "discovered_company", type_="foreignkey"
        )
    op.drop_index("ix_discovered_company_duplicate_of", table_name="discovered_company")
    op.drop_column("discovered_company", "merge_reason")
    op.drop_column("discovered_company", "merged_by")
    op.drop_column("discovered_company", "merged_at")
    op.drop_column("discovered_company", "duplicate_of")
    op.drop_column("discovered_company", "canonical_name")
