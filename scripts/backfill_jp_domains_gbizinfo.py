# 1회성(2026-08-27, git 미반영): EDINET 발견분 잔여 도메인을 gBizINFO(경산성 무료 API)로 백필.
"""법인번호로 gBizINFO 조회해 discovered_company(registry='edinet') 의 빈 domain 을 채운다.

Wikidata 백필(1,649건)의 잔여분 대상. 정부 공식 데이터라 오폭 위험 최소.
제약: 기존 domain 보유·타 행 점유 도메인은 스킵(2026-08-10 소유주 오폭 사고 계열 가드).
쓰로틀 0.35s/콜. 인증: LEADCRAWLER_GBIZINFO_API_TOKEN(.env).

사용: python scripts/backfill_jp_domains_gbizinfo.py [limit]
"""

from __future__ import annotations

import os
import sys
import time

import httpx
from sqlalchemy import select

from leadcrawler.config import Settings
from leadcrawler.dedup import normalize_domain
from leadcrawler.schema import DiscoveredCompanyRow
from leadcrawler.storage.db import get_sessionmaker

_API = "https://info.gbiz.go.jp/hojin/v1/hojin/{no}"


def main() -> int:
    token = os.environ.get("LEADCRAWLER_GBIZINFO_API_TOKEN", "").strip()
    if not token:
        print("[gbiz] LEADCRAWLER_GBIZINFO_API_TOKEN 없음 — .env 확인", flush=True)
        return 1
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    sm = get_sessionmaker(Settings())
    with sm() as s:
        taken = {
            d for (d,) in s.execute(
                select(DiscoveredCompanyRow.domain).where(DiscoveredCompanyRow.domain.is_not(None))
            )
        }
        rows = s.execute(
            select(DiscoveredCompanyRow)
            .where(DiscoveredCompanyRow.registry == "edinet")
            .where(DiscoveredCompanyRow.domain.is_(None))
            .where(DiscoveredCompanyRow.reg_no.is_not(None))
        ).scalars().all()
        if limit:
            rows = rows[:limit]
        print(f"[gbiz] 대상 {len(rows)}", flush=True)
        updated = miss = no_url = occupied = err = 0
        client = httpx.Client(headers={"X-hojinInfo-api-token": token}, timeout=20)
        for i, row in enumerate(rows, 1):
            try:
                r = client.get(_API.format(no=row.reg_no))
                if r.status_code != 200:
                    err += 1
                    if r.status_code in (401, 403, 429):
                        print(f"[gbiz] HTTP {r.status_code} — 중단(인증/쿼터)", flush=True)
                        break
                    continue
                infos = r.json().get("hojin-infos") or []
                url = (infos[0].get("company_url") or "") if infos else ""
                if not infos:
                    miss += 1
                elif not url:
                    no_url += 1
                else:
                    dom = normalize_domain(url)
                    if not dom:
                        no_url += 1
                    elif dom in taken:
                        occupied += 1
                    else:
                        row.domain = dom
                        taken.add(dom)
                        updated += 1
            except Exception as exc:  # 네트워크 단발 오류는 건너뛰고 계속.
                err += 1
                print(f"[gbiz] err {row.reg_no}: {str(exc)[:60]}", flush=True)
            if i % 100 == 0:
                s.commit()
                print(f"[gbiz] {i}/{len(rows)} | 갱신 {updated} · 미등재 {miss} · URL없음 "
                      f"{no_url} · 점유 {occupied} · 오류 {err}", flush=True)
            time.sleep(0.35)
        s.commit()
        print(f"[gbiz] 완료 — 갱신 {updated} · 미등재 {miss} · URL없음 {no_url}"
              f" · 점유 {occupied} · 오류 {err}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
