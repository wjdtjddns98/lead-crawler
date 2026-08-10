"""도메인 보유 미승격 발견행 승격 백필 (일회성 회수 잡).

대상 = ``discovered_company`` 에 **도메인은 있는데** ``company`` 행이 없는 회사.
발견 당시 실존검증까지 못 갔거나(파이프라인 중단·취소) 검증에서 떨어진 뒤 재시도가 없던
사각이다. 도메인이 이미 있으므로 **검색 API 를 전혀 안 쓴다** — 자기 사이트 HTTP 왕복만
든다(도메인 없는 회사를 되짚는 ``backfill-resolve-domains`` 와 대비되는 지점).

기존 파이프라인 함수(_build_lead/_persist_lead)를 그대로 재사용 — 제약②(실존만 저장)는
_persist_lead 가 그대로 강제하므로, 죽은 사이트는 여기서도 승격되지 않고 원장만 갱신된다.

멱등: 승격되면 다음 배치의 대상 쿼리에서 자동 제외된다(``co.id is null`` 조건). 중단해도
이어서 재실행하면 남은 것부터 계속한다.

사용:  python scripts/backfill_promote_domained.py [workers] [batch] [country ...]
       (기본 workers=3 — A/C 백필과 동시 구동 시 헤드리스 메모리 경합을 피하려 보수적.
        3번째 인자부터는 국가 코드 — 지정 시 그 국가만, 미지정=전세계. 예: ... 3 200 KR)

ponytail: 일회성 백필이라 CLI 커맨드화 안 함 — scripts/ 스크립트로 충분(backfill_reenrich
와 같은 판단). 상시화되면 그때 승격.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from leadcrawler.config import Settings
from leadcrawler.cost_ledger import CostLedger
from leadcrawler.dedup import normalize_domain
from leadcrawler.enrich.enricher import Enricher
from leadcrawler.enrich.industry_classify import build_classifier
from leadcrawler.pipeline.fill import _DOMAIN_OVERSHARE_CAP, _scoped
from leadcrawler.pipeline.run import _build_lead, _persist_lead
from leadcrawler.sources.base import DiscoveredCompany
from leadcrawler.storage.db import get_sessionmaker
from leadcrawler.verify.email_validator import EmailValidator
from leadcrawler.verify.existence import ExistenceVerifier
from leadcrawler.verify.registry_active import build_registry_checker

# 배치 LIMIT — 전량(2.8만)을 한 번에 메모리로 올리지 않고 끊어서 처리(재개 가능).
# {scope} 슬롯은 fill._scoped 가 국가 필터로 채운다(미지정=빈 문자열=전세계).
_COUNT_SQL = """
    select count(*)
    from discovered_company d
    left join company co on co.canonical_key = d.canonical_key
    where co.id is null and coalesce(d.domain, '') <> '' {scope}
    """

# canonical_key 커서로 전진한다. "대상에서 빠지는가"에 기대면 안 된다 — 죽은 사이트는
# 제약②로 company 가 안 생겨 co.id is null 을 계속 만족하므로, 같은 배치가 영원히 재선택된다.
# discovered_company 의 PK 는 canonical_key 다(id 컬럼 없음 — 2026-07-31 실사고).
_TARGET_SQL = """
    select d.canonical_key, d.name, d.country, d.industry, d.listed, d.domain,
           d.registry, d.registry_id, d.source, d.segment, d.reg_no, d.region,
           d.ticker, d.phone, d.ir_url, d.name_eng, d.address
    from discovered_company d
    left join company co on co.canonical_key = d.canonical_key
    where co.id is null and coalesce(d.domain, '') <> '' and d.canonical_key > :after
    {scope}
    order by d.canonical_key
    limit :limit
    """


def _load_domain_guards(session) -> tuple[set[str], set[str]]:  # noqa: ANN001 (Session)
    """도메인 dedup 가드 시드 — (기존 company 점유 도메인, 원장 과공유 도메인).

    제약①(2026-08-10 사고 수정): ① 기존 company 가 이미 쓰는 도메인은 재승격하지
    않는다(한 도메인 = 한 회사). ② 발견 원장에서 과공유된 도메인(디렉터리 신호,
    fill._DOMAIN_OVERSHARE_CAP)은 통째로 스킵한다 — 구버전은 이 가드가 없어 해석기가
    오채택한 디렉터리 도메인(nicebizinfo 등)을 수천 건씩 무차별 승격했다.
    """
    taken = {
        d for d in (
            normalize_domain(h) for (h,) in session.execute(
                text("select homepage from company where coalesce(homepage,'') <> ''")
            )
        ) if d is not None
    }
    # 원장 domain 은 소스에 따라 raw URL 표기가 섞일 수 있어(save_discovered 는 정규화
    # 안 함) SQL group by 대신 정규화 후 집계한다 — 표기 분산으로 캡을 우회하지 못하게
    # (교차리뷰 MED).
    counts: Counter[str] = Counter()
    for (dom,) in session.execute(
        text("select domain from discovered_company where coalesce(domain,'') <> ''")
    ):
        nd = normalize_domain(dom)
        if nd is not None:
            counts[nd] += 1
    overshared = {d for d, n in counts.items() if n >= _DOMAIN_OVERSHARE_CAP}
    return taken, overshared


def _dc_from_row(r) -> DiscoveredCompany:
    return DiscoveredCompany(
        canonical_key=r.canonical_key, name=r.name, country=r.country or "",
        industry=r.industry or "", listed=r.listed or "unknown", domain=r.domain,
        registry=r.registry, registry_id=r.registry_id, source=r.source or "",
        segment=r.segment, reg_no=r.reg_no, region=r.region, ticker=r.ticker,
        phone=r.phone, ir_url=r.ir_url, name_eng=r.name_eng, address=r.address,
    )


def main() -> int:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    batch = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    countries = sys.argv[3:] or None  # 국가 스코프 — 미지정=전세계(기존 동작 그대로).
    cnt_stmt, cnt_params = _scoped(_COUNT_SQL, countries)
    tgt_stmt, tgt_params = _scoped(_TARGET_SQL, countries)

    settings = Settings()
    if settings.dry_run:
        print("DRY_RUN=true — 백필 무의미. 중단.", flush=True)
        return 1
    sm = get_sessionmaker(settings)
    cost_ledger = CostLedger(settings, persist=True)
    registry_checker = build_registry_checker(settings)
    classifier = build_classifier(settings, ledger=cost_ledger)  # 스텝리스 공유 안전.

    with sm() as s:
        remaining = int(s.execute(cnt_stmt, cnt_params).scalar() or 0)
        taken, overshared = _load_domain_guards(s)
    print(
        f"[promote] 대상 {remaining} 곳 (workers={workers} batch={batch}"
        f" country={countries or '전세계'} | 도메인점유 {len(taken)} 과공유 {len(overshared)})",
        flush=True,
    )

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
            print(f"[promote] enrich 실패 {dc.canonical_key}: {type(exc).__name__}: {exc}", flush=True)
            return dc, None

    done = promoted = emails = 0
    after = ""  # 커서 — 재실행 시 처음부터 훑되 이미 승격된 행은 쿼리에서 빠진다.
    t0 = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            while True:
                with sm() as rd:
                    rows = rd.execute(
                        tgt_stmt, {**tgt_params, "limit": batch, "after": after}
                    ).all()
                if not rows:
                    break
                after = rows[-1].canonical_key  # 다음 배치는 이 키 뒤부터(고착 방지).
                targets = []
                for r in rows:
                    done += 1
                    dom = normalize_domain(r.domain)
                    if dom is None or dom in taken or dom in overshared:
                        continue  # 이미 점유/과공유 도메인 — enrich 비용도 쓰지 않는다.
                    taken.add(dom)  # 같은 런 안의 후속 중복(배치 내 포함)도 차단.
                    targets.append(_dc_from_row(r))
                with sm() as ws:
                    for dc, lead in pool.map(_work, targets):
                        if lead is None:
                            continue
                        _persist_lead(ws, dc, lead)
                        # 제약②: 실존(active)만 company 로 저장된다 — 승격 여부는 그걸로 센다.
                        if lead.company.is_active:
                            promoted += 1
                        if lead.email is not None:
                            emails += 1
                el = time.monotonic() - t0
                rate = done / el if el else 0
                eta = (remaining - done) / rate / 60 if rate else 0
                print(
                    f"[promote] {done}/{remaining} | 승격 {promoted} | 이메일 {emails} | "
                    f"{rate:.1f}/s | ETA {eta:.0f}분",
                    flush=True,
                )
    finally:
        for obj in created:
            close = getattr(obj, "close", None)
            if callable(close):
                close()
    print(f"[promote] 완료 — 처리 {done}, 승격 {promoted}, 이메일 {emails}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
