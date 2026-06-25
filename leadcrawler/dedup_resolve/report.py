"""배치 중복 리포트(C1) — 기존 발견 원장 위에서 도는 오프라인 잡.

수집 파이프라인과 무관하게 ``discovered_company`` 전건을 읽어 :mod:`near_dup` 사다리로
중복 후보 쌍을 뽑고, 사람/LLM 검토용 결정적 JSON 리포트로 저장한다. 네트워크·과금 없음.
이미 머지된 행(``duplicate_of`` 채워짐)은 기본 제외해 재실행 시 중복 보고를 막는다.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..schema import DiscoveredCompanyRow
from .near_dup import (
    MAX_BLOCK_SIZE,
    NAME_MEDIUM,
    NAME_STRONG,
    CompanyRecord,
    DuplicateCandidate,
    SkippedBlock,
    match_records,
)

# C1 비완전성 caveat — 운영자가 zero/소수 후보를 "중복 없음"으로 오해하지 않도록.
REPORT_NOTE = (
    "C1 배치 리포트는 무료·결정적 블로킹 기반이라 완전하지 않다. 이름과 도메인이 둘 다 "
    "다른 동일 기업은 못 잡으며 C2(LLM 판정)/C4(사람 워크벤치)로 위임된다. skipped_blocks "
    "가 있으면 해당 블록은 크기 초과로 비교를 생략한 것이니 --max-block 을 높여 재실행하라."
)


class DuplicateReport(BaseModel):
    """중복 리포트 1건 — 요약 + 후보 쌍 목록."""

    total_records: int  # 비교 대상이 된 발견 레코드 수
    total_candidates: int  # 찾은 중복 후보 쌍 수(keep_both 포함)
    by_tier: dict[str, int]  # 티어별 후보 수
    auto_removable: int  # 최상위(auto) 티어 — 자동제거 가능(가역)
    keep_both: int  # 동명이인 가능(둘 다 유지) — total 에 포함되나 해소 대상 아님(정보용)
    name_strong: float  # 사용한 임계값(이름 高)
    name_medium: float  # 사용한 임계값(이름 中)
    skipped_blocks: list[SkippedBlock]  # 크기 초과로 비교 생략된 블록(커버리지 공백 명시)
    note: str = REPORT_NOTE
    candidates: list[DuplicateCandidate]


def load_company_records(
    session: Session, *, include_merged: bool = False
) -> list[CompanyRecord]:
    """발견 원장에서 비교용 레코드를 적재한다(키·이름·국가·도메인만).

    ``include_merged`` 가 False(기본)면 이미 중복 판정돼 흡수된 행(``duplicate_of`` 채워짐)은
    제외한다 — 재실행 시 해소된 중복을 다시 보고하지 않기 위함.
    """
    stmt = select(
        DiscoveredCompanyRow.canonical_key,
        DiscoveredCompanyRow.name,
        DiscoveredCompanyRow.country,
        DiscoveredCompanyRow.domain,
    )
    if not include_merged:
        stmt = stmt.where(DiscoveredCompanyRow.duplicate_of.is_(None))
    return [
        CompanyRecord(key=key, name=name, country=country or "", domain=domain)
        for key, name, country, domain in session.execute(stmt).all()
    ]


def build_report(
    records: list[CompanyRecord],
    *,
    name_strong: float = NAME_STRONG,
    name_medium: float = NAME_MEDIUM,
    max_block_size: int = MAX_BLOCK_SIZE,
) -> DuplicateReport:
    """레코드 목록에서 중복 후보를 찾아 요약과 함께 리포트로 만든다(결정적)."""
    result = match_records(
        records,
        name_strong=name_strong,
        name_medium=name_medium,
        max_block_size=max_block_size,
    )
    candidates = result.candidates
    by_tier = dict(Counter(c.tier for c in candidates))
    return DuplicateReport(
        total_records=len(records),
        total_candidates=len(candidates),
        by_tier=by_tier,
        auto_removable=by_tier.get("auto", 0),
        keep_both=by_tier.get("keep_both", 0),
        name_strong=name_strong,
        name_medium=name_medium,
        skipped_blocks=result.skipped_blocks,
        candidates=candidates,
    )


def write_report(report: DuplicateReport, path: str | Path) -> Path:
    """리포트를 결정적 JSON 파일로 저장하고 경로를 반환한다(부모 디렉터리 자동 생성)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return p


def run_dedup_report(
    session: Session,
    out: str | Path,
    *,
    include_merged: bool = False,
    name_strong: float = NAME_STRONG,
    name_medium: float = NAME_MEDIUM,
    max_block_size: int = MAX_BLOCK_SIZE,
) -> DuplicateReport:
    """DB 적재 → 리포트 빌드 → JSON 저장을 한 번에 수행한다(CLI 진입점용)."""
    records = load_company_records(session, include_merged=include_merged)
    report = build_report(
        records,
        name_strong=name_strong,
        name_medium=name_medium,
        max_block_size=max_block_size,
    )
    write_report(report, out)
    return report
