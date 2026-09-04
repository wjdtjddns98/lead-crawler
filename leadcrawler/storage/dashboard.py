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

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..schema import CompanyRow, ContactRow, DiscoveredCompanyRow, ReviewQueueRow
from ..sources.countries import resolve_country
from ..sources.taxonomy import UNCLASSIFIED
from .review import CONFIRMED, PENDING, REJECTED


def holdings_summary(session: Session) -> dict:
    """원장·회사·큐 보유 현황 스냅샷 — /dashboard/summary 응답 본체.

    - ledger: 발견 원장 4분면 — total = promoted + domained_unpromoted +
      undomained_unpromoted + absorbed. absorbed(dedup 흡수, ``duplicate_of`` 기록)는
      다른 키로 이미 대표되는 행이라 승격 백로그가 아니다(리뷰 HIGH — 섞으면 백로그가
      영구 과대집계됨). 4분면을 **단일 문장** 조건부 집계로 산출해 불변식이 문장 내에서
      원자적으로 성립한다(READ COMMITTED 에서 문장 간 끼어들기 무관).
    - companies: 승격(실존) 회사 수·이메일 보유 수와 국가/업종별 분포(n 내림차순).
      발견 원장 없이 존재하는 고아 company 가 있으면 ``companies.total`` 이
      ``ledger.promoted`` 를 넘을 수 있다(queue_stock 과 동일 현상 — 버그 아님).
    - queue: 검증 큐 상태별 카운트(pending 은 점유 여부로 분해).
    """
    absorbed = DiscoveredCompanyRow.duplicate_of.is_not(None)
    live = ~absorbed
    unpromoted = live & CompanyRow.id.is_(None)
    domain_blank = func.trim(func.coalesce(DiscoveredCompanyRow.domain, "")) == ""
    one = lambda pred: func.coalesce(func.sum(case((pred, 1), else_=0)), 0)  # noqa: E731
    total, n_promoted, n_domained, n_undomained, n_absorbed = session.execute(
        select(
            func.count(),
            one(live & CompanyRow.id.is_not(None)),
            one(unpromoted & ~domain_blank),
            one(unpromoted & domain_blank),
            one(absorbed),
        )
        .select_from(DiscoveredCompanyRow)
        .outerjoin(CompanyRow, CompanyRow.canonical_key == DiscoveredCompanyRow.canonical_key)
    ).one()
    ledger = {
        "total": int(total),
        "promoted": int(n_promoted),
        "domained_unpromoted": int(n_domained),
        "undomained_unpromoted": int(n_undomained),
        "absorbed": int(n_absorbed),
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
        if status == PENDING:
            queue["pending_unclaimed" if unclaimed else "pending_claimed"] += int(n)
        elif status in (CONFIRMED, REJECTED):
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
