"""미승격 기존 임포트 행(source='import') 업종 LLM 분류 백필 — 2026-08-19 PO 지시.

기존 제공 엑셀 시드는 업종을 안 담아 미승격분 ~20,910행이 전량 미분류다. 승격을 기다리지
않고 원장(discovered_company) 단계에서 분류를 소급한다 — cli.backfill_industries(company
대상)와 같은 규칙:
  · 홈페이지 본문이 있을 때만 분류(블라인드 분류 금지 — 없으면 스킵, 미분류 유지)
  · 분류기 abstain(None)·닫힌 택소노미 밖 값은 갱신 안 함 → 반복 실행 멱등
  · commit_every 건마다 중간 커밋(중단 시 지출분 보존)
승격 시 파이프라인이 재분류할 수 있지만 정본 규칙상 확신 라벨은 유지된다.

사용:  python scripts/backfill_import_industry.py [--limit 0] [--commit-every 50]

ponytail: 1회성 백필이라 CLI 커맨드화 안 함(backfill_confirm_unlisted 와 같은 판단).
순차 처리 — fetch 지배적이라 느리지만(행당 ~1s) 멱등 재실행으로 나눠 돌릴 수 있다.
"""

from __future__ import annotations

import argparse
import time

import httpx
from sqlalchemy import select

from leadcrawler.config import Settings
from leadcrawler.cost_ledger import CostLedger
from leadcrawler.enrich.industry_classify import build_classifier
from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow
from leadcrawler.sources.taxonomy import AMBIGUOUS_LABELS
from leadcrawler.storage.db import get_sessionmaker


def main() -> int:
    ap = argparse.ArgumentParser(description="미승격 import 행 업종 분류 백필")
    ap.add_argument("--limit", type=int, default=0, help="처리 상한(0=전체) — 소량 시험용")
    ap.add_argument("--commit-every", type=int, default=50)
    args = ap.parse_args()

    # 기본 캡(industry_llm_max_calls=5000)이 대상 2만행보다 작아 절반이 조용히 무산된다
    # (리뷰 HIGH) — 이 런의 목적이 전량 분류이므로 캡을 대상보다 크게 올린다(월예산
    # 가드는 ledger 가 그대로 집행).
    settings = Settings(industry_llm_max_calls=50_000)
    if settings.dry_run:
        print("DRY_RUN=true — 과금 분류 백필 무의미. 중단.", flush=True)
        return 1
    ledger = CostLedger(settings, persist=True)
    classifier = build_classifier(settings, ledger=ledger)

    sm = get_sessionmaker(settings)
    with httpx.Client(
        timeout=10, follow_redirects=True,
        headers={"User-Agent": settings.discovery_user_agent},
    ) as client, sm() as s:
        rows = s.execute(
            select(DiscoveredCompanyRow)
            .outerjoin(CompanyRow, CompanyRow.canonical_key == DiscoveredCompanyRow.canonical_key)
            .where(
                DiscoveredCompanyRow.source == "import",
                CompanyRow.id.is_(None),
                # 선례(cli.backfill_industries)와 동일 대상: 미분류 + catch-all(기타 제조).
                DiscoveredCompanyRow.industry.in_(["", *AMBIGUOUS_LABELS]),
                DiscoveredCompanyRow.domain.is_not(None),
            )
            .order_by(DiscoveredCompanyRow.canonical_key)
            .limit(args.limit or None)
        ).scalars().all()
        print(f"[import-industry] 대상 {len(rows):,}행 (도메인 보유·미승격·미분류)", flush=True)

        done = updated = starved = 0
        t0 = time.monotonic()
        for d in rows:
            done += 1
            html = None
            # www 폴백은 enricher._home_fetch 실측 패턴(naked 도메인에 A레코드 없는 사이트).
            hosts = [d.domain] if d.domain.startswith("www.") else [d.domain, f"www.{d.domain}"]
            for host in hosts:
                try:
                    r = client.get(f"https://{host}")
                    if r.status_code < 400:
                        html = r.text
                        break
                except Exception:
                    continue  # 죽은/느린 사이트 — 홈페이지 게이트에 걸려 스킵된다.
            if not html:
                continue  # 블라인드 분류 금지 — 미분류 유지, 재실행 시 재시도.
            verdict = classifier.classify(d.name, d.domain, html)
            if verdict.label and verdict.label != d.industry:
                d.industry = verdict.label
                updated += 1
            # 캡/예산 소진이면 abstain 이 무과금으로 연속된다 — fetch 낭비 말고 조기 종료
            # (리뷰 MED). 일시 오류 섞임을 감안해 연속 30회에서 끊는다.
            starved = starved + 1 if (verdict.label is None and not verdict.billed) else 0
            if starved >= 30:
                print("[import-industry] LLM 캡/예산 소진 추정 — 조기 종료(재실행 시 이어서)",
                      flush=True)
                break
            if args.commit_every and done % args.commit_every == 0:
                s.commit()  # 중단돼도 여기까지 지출분은 보존.
            if done % 100 == 0:
                rate = done / (time.monotonic() - t0)
                eta = (len(rows) - done) / rate / 60 if rate else 0
                print(
                    f"[import-industry] {done}/{len(rows)} | 분류 {updated}"
                    f" | {rate:.1f}/s | ETA {eta:.0f}분", flush=True,
                )
        s.commit()
        print(f"[import-industry] 완료 — 검토 {done:,}, 분류 {updated:,}"
              f" (스킵분은 재실행 시 재시도)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
