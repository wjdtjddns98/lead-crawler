"""inline 중복 승격(C5) — 수집 파이프라인이 신규 리드를 적재할 때 기존 원장과 즉시 대조.

정확 dedup(``seen``/``seen_domains``, 제약①)을 통과한 신규 리드도, 표기 차이로 다른
``canonical_key`` 를 받았을 뿐 사실 **이미 원장에 있는 기업**일 수 있다. 이 모듈은 그런
리드를 near_dup 사다리의 **최상위(auto) 티어**(이름 高 + 도메인root 일치)일 때만 inline
으로 잡아, 재추출 없이 기존 생존 레코드에 흡수시킨다(제약① 재추출 방지).

보수성(제약② 리드손실 방지): auto 티어만 자동 흡수한다. 도메인이 없거나 도메인root 가
다르면 auto 가 성립하지 않으므로 inline 은 **아무것도 하지 않고** 정상 추출로 흘려보낸다
(경계 케이스는 배치 리포트 C1/판정 C2/워크벤치 C4 가 처리). 결정적·네트워크 없음.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..dedup import normalize_domain
from ..logging import get_logger
from ..sources.base import DiscoveredCompany
from .near_dup import NAME_STRONG, CompanyRecord, match_records

log = get_logger("dedup.inline")


def find_inline_duplicate(
    session: Session, dc: DiscoveredCompany, *, name_strong: float = NAME_STRONG
) -> str | None:
    """기존 원장에서 ``dc`` 와 auto-티어 중복인 **생존** 레코드 key 를 찾는다(없으면 None).

    auto 티어는 도메인root 동치를 요구하므로, ``dc`` 도메인root 와 같은 후보만 블로킹으로
    좁혀 비교한다(전건 스캔 회피). 도메인이 없으면 auto 불가라 즉시 None.
    """
    root = normalize_domain(dc.domain)
    if not root:
        return None  # auto 티어는 도메인 동치 필요 — 도메인 없으면 inline 대상 아님

    from ..schema import DiscoveredCompanyRow

    # 같은 국가 + 도메인root 가 같을 후보(생존자만)로 좁힌다. ilike 는 1차 필터, 정밀
    # 동치는 아래 normalize_domain 비교로 확정(www. 등 표기차·부분일치 오탐 제거).
    rows = session.execute(
        select(
            DiscoveredCompanyRow.canonical_key,
            DiscoveredCompanyRow.name,
            DiscoveredCompanyRow.country,
            DiscoveredCompanyRow.domain,
        ).where(
            DiscoveredCompanyRow.country == (dc.country or ""),
            DiscoveredCompanyRow.duplicate_of.is_(None),
            DiscoveredCompanyRow.canonical_key != dc.canonical_key,
            DiscoveredCompanyRow.domain.is_not(None),
            DiscoveredCompanyRow.domain.ilike(f"%{root}%"),
        )
    ).all()
    candidates = [
        CompanyRecord(key=k, name=n, country=c or "", domain=d)
        for k, n, c, d in rows
        if normalize_domain(d) == root  # 정밀 도메인root 동치만
    ]
    if not candidates:
        return None

    new_rec = CompanyRecord(
        key=dc.canonical_key, name=dc.name, country=dc.country or "", domain=dc.domain
    )
    result = match_records([new_rec, *candidates], name_strong=name_strong)
    for cand in result.candidates:
        if cand.tier == "auto" and dc.canonical_key in (cand.key_a, cand.key_b):
            survivor = cand.key_b if cand.key_a == dc.canonical_key else cand.key_a
            log.info(
                "dedup.inline.match",
                key=dc.canonical_key,
                survivor=survivor,
                score=cand.name_score,
            )
            return survivor
    return None
