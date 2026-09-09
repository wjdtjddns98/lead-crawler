# 1회성(2026-08-26, git 미반영): EDINET 발견분(JP 상장사)에 Wikidata 공식 웹사이트 백필.
"""ticker 매칭으로 discovered_company(registry='edinet') 의 빈 domain 을 채운다.

입력: {ticker: website_url} JSON (Wikidata TSE P414+P856 실측 — 세션 스크래치패드 산출).
제약: 기존 domain 이 있으면 건드리지 않는다. 도메인이 이미 타 행에 점유(canonical/원장)면
스킵 — 소유주 오폭 방지(2026-08-10 사고 계열). Serper 소모 0 이 목적.

사용: python scripts/backfill_jp_domains_wikidata.py <json경로>
"""

from __future__ import annotations

import json
import sys

from sqlalchemy import select

from leadcrawler.config import Settings
from leadcrawler.dedup import normalize_domain
from leadcrawler.schema import DiscoveredCompanyRow
from leadcrawler.storage.db import get_sessionmaker


def main() -> int:
    mapping: dict[str, str] = json.load(open(sys.argv[1], encoding="utf-8"))
    sm = get_sessionmaker(Settings())
    updated = skipped_has = skipped_occupied = no_row = bad = 0
    with sm() as s:
        # 도메인 점유 여부 판정용 — 원장 전체의 기존 도메인 집합(소유주 오폭 방지).
        taken = {
            d for (d,) in s.execute(
                select(DiscoveredCompanyRow.domain).where(DiscoveredCompanyRow.domain.is_not(None))
            )
        }
        rows = s.execute(
            select(DiscoveredCompanyRow).where(DiscoveredCompanyRow.registry == "edinet")
        ).scalars().all()
        by_ticker = {r.ticker: r for r in rows if r.ticker}
        for ticker, url in mapping.items():
            row = by_ticker.get(ticker)
            if row is None:
                no_row += 1
                continue
            if row.domain:
                skipped_has += 1
                continue
            dom = normalize_domain(url)
            if not dom:
                bad += 1
                continue
            if dom in taken:
                skipped_occupied += 1
                continue
            row.domain = dom
            taken.add(dom)
            updated += 1
        s.commit()
    print(
        f"[jp-wikidata] 갱신 {updated} · 기보유 {skipped_has} · 점유스킵 {skipped_occupied}"
        f" · 행없음 {no_row} · URL불량 {bad}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
