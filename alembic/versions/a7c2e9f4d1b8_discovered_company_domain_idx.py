"""discovered_company.domain 인덱스 — 해석 배치 과공유 COUNT 풀스캔 제거

Revision ID: a7c2e9f4d1b8
Revises: d1f4a8c6e2b9
Create Date: 2026-08-27 15:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c2e9f4d1b8"
down_revision: str | None = "d1f4a8c6e2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_discovered_company_domain", "discovered_company", ["domain"])


def downgrade() -> None:
    op.drop_index("ix_discovered_company_domain", table_name="discovered_company")
