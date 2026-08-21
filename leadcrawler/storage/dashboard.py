"""보유 데이터 대시보드 집계 — 원장·승격 회사·큐를 한 응답으로(2026-08-21 PO 지시).

"DB 하나로 통합해 보유 데이터를 한눈에" 트랙의 백엔드 절반: 물리 통합(전량 승격 백필)과
별개로, 발견 원장(discovered_company)·승격 회사(company)·검증 큐(review_queue)의 현황을
단일 스냅샷으로 집계해 FE 대시보드에 준다.

축·접기 규칙은 :func:`review.queue_stock` 과 동일 어휘를 쓴다(국가=ISO2 접기·빈값 원문,
업종 빈값='미분류') — 대시보드 숫자를 큐 필터로 그대로 되짚을 수 있게(왕복 대칭).

ponytail: 요청 시점 스냅샷 집계(원장 group by 수회, 각 ~100ms대) — 캐시 없음.
폴링 용도가 아니며(대시보드 진입 시 1회), 느려지면 그때 materialized 집계로.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..schema import CompanyRow, ContactRow, DiscoveredCompanyRow, ReviewQueueRow
from ..sources.countries import resolve_country
from ..sources.taxonomy import UNCLASSIFIED


def holdings_summary(session: Session) -> dict:
    """원장·회사·큐 보유 현황 스냅샷 — /dashboard/summary 응답 본체.

    - ledger: 발견 원장 전체와 승격 여부·도메인 유무 분해(total = promoted +
      domained_unpromoted + undomained_unpromoted).
    - companies: 승격(실존) 회사 수·이메일 보유 수와 국가/업종별 분포(n 내림차순).
    - queue: 검증 큐 상태별 카운트(pending 은 점유 여부로 분해).
    """
    promoted = (
        select(func.count())
        .select_from(DiscoveredCompanyRow)
        .join(CompanyRow, CompanyRow.canonical_key == DiscoveredCompanyRow.canonical_key)
    )
    unpromoted_base = (
        select(func.count())
        .select_from(DiscoveredCompanyRow)
        .outerjoin(CompanyRow, CompanyRow.canonical_key == DiscoveredCompanyRow.canonical_key)
        .where(CompanyRow.id.is_(None))
    )
    domain_blank = func.coalesce(DiscoveredCompanyRow.domain, "") == ""
    ledger = {
        "total": session.execute(
            select(func.count()).select_from(DiscoveredCompanyRow)
        ).scalar_one(),
        "promoted": session.execute(promoted).scalar_one(),
        "domained_unpromoted": session.execute(
            unpromoted_base.where(~domain_blank)
        ).scalar_one(),
        "undomained_unpromoted": session.execute(
            unpromoted_base.where(domain_blank)
        ).scalar_one(),
    }

    companies_total = session.execute(
        select(func.count()).select_from(CompanyRow)
    ).scalar_one()
    with_email = session.execute(
        select(func.count(func.distinct(ContactRow.company_id))).where(
            ContactRow.type == "email"
        )
    ).scalar_one()

    by_country: dict[str, int] = {}
    for country, n in session.execute(
        select(CompanyRow.country, func.count()).group_by(CompanyRow.country)
    ).all():
        resolved = resolve_country(country or "")
        label = resolved.iso2 if resolved else (country or "").strip()
        by_country[label] = by_country.get(label, 0) + int(n)

    by_industry: dict[str, int] = {}
    for industry, n in session.execute(
        select(CompanyRow.industry, func.count()).group_by(CompanyRow.industry)
    ).all():
        label = (industry or "").strip() or UNCLASSIFIED
        by_industry[label] = by_industry.get(label, 0) + int(n)

    def _ranked(agg: dict[str, int], key: str) -> list[dict]:
        return [
            {key: k, "n": n}
            for k, n in sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    queue_rows = session.execute(
        select(
            ReviewQueueRow.status,
            ReviewQueueRow.claimed_by.is_(None),
            func.count(),
        ).group_by(ReviewQueueRow.status, ReviewQueueRow.claimed_by.is_(None))
    ).all()
    queue = {"pending_unclaimed": 0, "pending_claimed": 0, "confirmed": 0, "rejected": 0}
    for status, unclaimed, n in queue_rows:
        if status == "pending":
            queue["pending_unclaimed" if unclaimed else "pending_claimed"] += int(n)
        elif status in ("confirmed", "rejected"):
            queue[status] += int(n)

    return {
        "ledger": ledger,
        "companies": {
            "total": companies_total,
            "with_email": with_email,
            "by_country": _ranked(by_country, "country"),
            "by_industry": _ranked(by_industry, "industry"),
        },
        "queue": queue,
    }
