"""기존 업종 라벨을 대분류 택소노미로 정규화(데이터 백필)

구체 업종 크롤이 세그먼트 라벨("제약"·"바이오" 등)을 그대로 저장해 큐/엑셀 필터
("제약·바이오" 정확일치)에 안 걸리던 파편화를 청소한다. 매핑은
leadcrawler.sources.industry._OLD_TO_TAXO 스냅샷(마이그레이션은 앱 코드를 import
하지 않는다 — 이후 표가 바뀌어도 이 리비전은 불변). 대소문자 무시 일치만 갱신하고
자유텍스트·이미 정규화된 라벨은 건드리지 않는다. 데이터 전용이라 downgrade 는 no-op
(원 라벨 소실 — 되돌릴 필요도 없음).

Revision ID: c5d8e1f4a7b2
Revises: a3f7c1d9e5b2
Create Date: 2026-07-03 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d8e1f4a7b2"
down_revision: str | None = "a3f7c1d9e5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 구 라벨(소문자) → 택소노미 라벨. sources/industry.py _OLD_TO_TAXO 의 2026-07-03 스냅샷.
_OLD_TO_TAXO: dict[str, str] = {
    "건설": "건설·엔지니어링",
    "construction": "건설·엔지니어링",
    "바이오": "제약·바이오",
    "제약": "제약·바이오",
    "pharma": "제약·바이오",
    "소프트웨어": "IT·소프트웨어",
    "software": "IT·소프트웨어",
    "it": "IT·소프트웨어",
    "유통": "유통·도소매",
    "도소매": "유통·도소매",
    "운송": "물류·운송",
    "물류": "물류·운송",
    "에너지": "에너지·전력",
    "energy": "에너지·전력",
    "부동산": "부동산·개발",
    "식품": "식품·음료",
    "화학": "화학·석유화학",
    "자동차": "자동차·모빌리티",
    "반도체": "반도체·디스플레이",
    "semiconductor": "반도체·디스플레이",
    "통신": "통신·네트워크",
}


def upgrade() -> None:
    conn = op.get_bind()
    for table in ("discovered_company", "company"):
        for old, taxo in _OLD_TO_TAXO.items():
            conn.execute(
                sa.text(f"UPDATE {table} SET industry = :taxo WHERE lower(industry) = :old"),
                {"taxo": taxo, "old": old},
            )


def downgrade() -> None:
    pass  # 데이터 전용 정규화 — 원 라벨 복원 불가·불필요(no-op).
