"""무이메일 생존사이트 회사 재-enrich 백필 (일회성 회수 잡).

헤드리스(Playwright)·OCR 미설치로 이메일 escalation 이 죽어 있던 기간에 site_alive 인데
이메일을 못 뽑은 회사를 다시 enrich 한다. 기존 파이프라인 함수(_build_lead/_persist_lead)를
그대로 재사용 — 저장·큐 적재(enqueue_email_review)·멱등 upsert 는 save_lead 가 처리한다.

멱등: save_lead 는 upsert 라 재실행 안전. 타깃 쿼리가 '이메일 없는 회사'만 잡으므로 이미
회수된 회사는 다음 실행에서 자동 제외된다. 진행상황은 stdout(+ 로그파일 리다이렉트)로.

ponytail: 일회성 백필이라 CLI 커맨드화 안 함 — scripts/ 스크립트로 충분. 상시화되면 그때 승격.
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from leadcrawler.config import Settings
from leadcrawler.cost_ledger import CostLedger
from leadcrawler.enrich.enricher import Enricher
from leadcrawler.enrich.industry_classify import build_classifier
from leadcrawler.pipeline.run import _build_lead, _persist_lead
from leadcrawler.sources.base import DiscoveredCompany
from leadcrawler.storage.db import get_sessionmaker
from leadcrawler.verify.email_validator import EmailValidator
from leadcrawler.verify.existence import ExistenceVerifier
from leadcrawler.verify.registry_active import build_registry_checker

# KR 국가 표기(별칭 포함) — DB 에 'KR'/'대한민국' 혼재.
_KR = ("KR", "대한민국")

_TARGET_SQL = text(
    """
    select d.canonical_key, d.name, d.country, d.industry, d.listed, d.domain,
           d.registry, d.registry_id, d.source, d.segment, d.reg_no, d.region,
           d.ticker, d.phone, d.ir_url, d.name_eng, d.address
    from company co
    join discovered_company d on d.canonical_key = co.canonical_key
    where co.site_alive is true
      and co.country = any(:kr)
      and coalesce(d.domain, '') <> ''
      and not exists (
          select 1 from contact ct where ct.company_id = co.id and ct.type = 'email'
      )
    """
)


def _dc_from_row(r) -> DiscoveredCompany:
    return DiscoveredCompany(
        canonical_key=r.canonical_key, name=r.name, country=r.country or "",
        industry=r.industry or "", listed=r.listed or "unknown", domain=r.domain,
        registry=r.registry, registry_id=r.registry_id, source=r.source or "",
        segment=r.segment, reg_no=r.reg_no, region=r.region, ticker=r.ticker,
        phone=r.phone, ir_url=r.ir_url, name_eng=r.name_eng, address=r.address,
    )


def main() -> int:
    settings = Settings()
    if settings.dry_run:
        print("DRY_RUN=true — 백필 무의미. 중단.", flush=True)
        return 1
    sm = get_sessionmaker(settings)
    cost_ledger = CostLedger(settings, persist=True)
    registry_checker = build_registry_checker(settings)
    classifier = build_classifier(settings, ledger=cost_ledger)  # 스텝리스 공유 안전.

    with sm() as read_session:
        rows = read_session.execute(_TARGET_SQL, {"kr": list(_KR)}).all()
    targets = [_dc_from_row(r) for r in rows]
    total = len(targets)
    print(f"[backfill] 대상 {total} 곳 재-enrich 시작 (workers={settings.enrich_workers})", flush=True)

    # 워커별 독립 인스턴스(공유 throttle 경쟁 회피) — run_pipeline 패턴 미러링.
    tl = threading.local()
    created: list[object] = []
    lock = threading.Lock()

    def _components():
        if not hasattr(tl, "enr"):
            tl.enr = Enricher(settings, cost_ledger=cost_ledger)
            tl.exi = ExistenceVerifier(settings, registry_checker=registry_checker)
            tl.val = EmailValidator(settings, cost_ledger=cost_ledger)
            with lock:
                created.extend([tl.enr, tl.exi, tl.val])
        return tl.enr, tl.exi, tl.val

    def _work(dc: DiscoveredCompany):
        enr, exi, val = _components()
        try:
            return dc, _build_lead(
                dc, enricher=enr, existence=exi, email_validator=val, classifier=classifier
            )
        except Exception as exc:  # 1건 실패가 배치를 안 죽이게(제약② 격리).
            print(f"[backfill] enrich 실패 {dc.canonical_key}: {type(exc).__name__}: {exc}", flush=True)
            return dc, None

    done = emails = 0
    t0 = time.monotonic()
    with sm() as write_session:
        with ThreadPoolExecutor(max_workers=max(1, settings.enrich_workers)) as pool:
            for dc, lead in pool.map(_work, targets):
                done += 1
                if lead is not None:
                    # 커밋 성공 + 실존 저장분만 이메일로 집계(_persist_lead bool 반환).
                    if (
                        _persist_lead(write_session, dc, lead)
                        and lead.company.is_active
                        and lead.email is not None
                    ):
                        emails += 1
                if done % 50 == 0 or done == total:
                    el = time.monotonic() - t0
                    rate = done / el if el else 0
                    eta = (total - done) / rate / 60 if rate else 0
                    print(
                        f"[backfill] {done}/{total} | 신규이메일 {emails} | "
                        f"{rate:.1f}/s | ETA {eta:.0f}분",
                        flush=True,
                    )

    for obj in created:
        close = getattr(obj, "close", None)
        if callable(close):
            close()
    print(f"[backfill] 완료 — 처리 {done}, 신규이메일 회수 {emails}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
