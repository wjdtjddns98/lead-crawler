"""이메일 채우기 consumer — 큐에 쌓인 '실존·무이메일' 회사에 이메일을 배치 병렬로 채운다.

발견(producer)과 이메일 보강(consumer)을 분리한 파이프라인의 소비자 단계. 발견 크롤이
``discovery_only`` 로 빠르게 회사+홈페이지를 큐에 쌓으면, 이 소비자가 무이메일 회사를
배치로 잡아 헤드리스/OCR 까지 돌려 이메일을 채우고 DB 를 갱신한다(멱등 — 채워지면 대상에서
빠짐). 기존 파이프라인 함수(_build_lead/_persist_lead)를 그대로 재사용한다.

CLI ``leadcrawler fill-emails [--loop]`` 가 이 모듈을 구동한다.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from ..config import Settings
from ..cost_ledger import CostLedger
from ..dedup import normalize_domain
from ..enrich.enricher import Enricher
from ..enrich.industry_classify import build_classifier
from ..logging import get_logger
from ..sources.base import DiscoveredCompany
from ..sources.domain_resolver import DomainResolver
from ..sources.http import HostRateLimiters
from ..storage.repository import backfill_domain, load_seen_domains
from ..verify.email_validator import EmailValidator
from ..verify.existence import ExistenceVerifier
from ..verify.registry_active import build_registry_checker
from .run import _build_lead, _persist_lead

log = get_logger("pipeline.fill")

# 대상 = site_alive(실존)·도메인 보유·이메일 연락처 없음. discovered_company 를 조인해
# enrich 에 필요한 발견필드(도메인·등록처·업종 등)를 함께 가져온다. 채워지면 자동 이탈(멱등).
_TARGET_SQL = text(
    """
    select d.canonical_key, d.name, d.country, d.industry, d.listed, d.domain,
           d.registry, d.registry_id, d.source, d.segment, d.reg_no, d.region,
           d.ticker, d.phone, d.ir_url, d.name_eng, d.address
    from company co
    join discovered_company d on d.canonical_key = co.canonical_key
    where co.site_alive is true
      and coalesce(d.domain, '') <> ''
      and not exists (
          select 1 from contact ct where ct.company_id = co.id and ct.type = 'email'
      )
    order by co.id
    limit :limit
    """
)
_COUNT_SQL = text(
    """
    select count(*) from company co
    join discovered_company d on d.canonical_key = co.canonical_key
    where co.site_alive is true and coalesce(d.domain, '') <> ''
      and not exists (select 1 from contact ct where ct.company_id = co.id and ct.type = 'email')
    """
)


def _dc_from_row(r) -> DiscoveredCompany:  # noqa: ANN001 (SQLAlchemy Row)
    return DiscoveredCompany(
        canonical_key=r.canonical_key, name=r.name, country=r.country or "",
        industry=r.industry or "", listed=r.listed or "unknown", domain=r.domain,
        registry=r.registry, registry_id=r.registry_id, source=r.source or "",
        segment=r.segment, reg_no=r.reg_no, region=r.region, ticker=r.ticker,
        phone=r.phone, ir_url=r.ir_url, name_eng=r.name_eng, address=r.address,
    )


def count_targets(sm: sessionmaker) -> int:
    """현재 이메일 채우기 대상(실존·무이메일) 회사 수."""
    with sm() as s:
        return int(s.execute(_COUNT_SQL).scalar() or 0)


def fill_batch(settings: Settings, sm: sessionmaker, *, limit: int, workers: int) -> tuple[int, int]:
    """대상 최대 ``limit`` 개를 ``workers`` 병렬로 enrich 해 이메일을 채운다.

    반환 (처리수, 신규이메일수). enrich(_build_lead)는 워커스레드에서, DB 적재(_persist_lead)는
    메인스레드 단독(파이프라인 계약). 1건 실패는 격리(배치 전체 보호). 멱등이라 재호출 안전.
    """
    with sm() as rd:
        targets = [_dc_from_row(r) for r in rd.execute(_TARGET_SQL, {"limit": limit}).all()]
    if not targets:
        return 0, 0

    cost_ledger = CostLedger(settings, persist=True)
    # 공유 호스트 캡 — CH `/company` 조회가 워커 수와 무관하게 합산 2req/s 를 지키게(429 방지).
    registry_checker = build_registry_checker(
        settings, rate_limiters=HostRateLimiters(default_rate=settings.discovery_rate_per_host)
    )
    classifier = build_classifier(settings, ledger=cost_ledger)  # 스텝리스 공유 안전.
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
            log.info("fill.enrich.error", key=dc.canonical_key, err=str(exc))
            return dc, None

    processed = emails = 0
    with sm() as ws:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for dc, lead in pool.map(_work, targets):
                processed += 1
                if lead is not None:
                    _persist_lead(ws, dc, lead)
                    if lead.email is not None:
                        emails += 1

    for obj in created:
        close = getattr(obj, "close", None)
        if callable(close):
            close()
    return processed, emails


# 대상 = 도메인 미보유 + 미승격(company 행 없음) 발견 행. 최초 발견 때 도메인을 못 준
# 소스(NPS 등)가 dedup 시드로 영원히 정체되는 사각(위 backfill_domain 독스트링 참고)을
# 최근 발견 우선으로 되짚어 도메인 해석부터 다시 시도한다.
_RESOLVE_TARGET_SQL = text(
    """
    select d.canonical_key, d.name, d.country, d.industry, d.listed, d.domain,
           d.registry, d.registry_id, d.source, d.segment, d.reg_no, d.region,
           d.ticker, d.phone, d.ir_url, d.name_eng, d.address
    from discovered_company d
    left join company co on co.canonical_key = d.canonical_key
    where coalesce(d.domain, '') = '' and co.id is null
    order by d.last_crawled_at desc
    limit :limit
    """
)
_RESOLVE_COUNT_SQL = text(
    """
    select count(*) from discovered_company d
    left join company co on co.canonical_key = d.canonical_key
    where coalesce(d.domain, '') = '' and co.id is null
    """
)


def count_resolve_targets(sm: sessionmaker) -> int:
    """현재 도메인 해석 대상(도메인없음·미승격) 회사 수."""
    with sm() as s:
        return int(s.execute(_RESOLVE_COUNT_SQL).scalar() or 0)


def resolve_batch(settings: Settings, sm: sessionmaker, *, limit: int, workers: int) -> tuple[int, int, int]:
    """대상 최대 ``limit`` 개의 도메인을 해석해 채우고, 실존이면 승격까지 시도한다.

    반환 (처리수, 도메인해석수, 신규승격수). 도메인 해석(worker)은 순수 네트워크
    호출이라 스레드별 독립 :class:`DomainResolver` 로 병렬화하고(``fill_batch`` 와
    동일 스레드로컬 관례), **도메인 동치 dedup(제약①)은 메인스레드에서 순차 판정**한다
    — 워커가 해석한 도메인이 이번 배치 안에서 서로 겹치거나(다른 표기의 같은 회사)
    이미 원장에 있는 도메인과 겹치면(``load_seen_domains`` 스냅샷) 승격을 건너뛰어
    중복 ``company`` 행을 막는다. 도메인 자체는 겹쳐도 항상 기록한다(사실 보존 —
    다음 배치가 같은 행을 다시 시도하지 않게).
    """
    with sm() as rd:
        targets = [_dc_from_row(r) for r in rd.execute(_RESOLVE_TARGET_SQL, {"limit": limit}).all()]
    if not targets:
        return 0, 0, 0

    cost_ledger = CostLedger(settings, persist=True)
    registry_checker = build_registry_checker(
        settings, rate_limiters=HostRateLimiters(default_rate=settings.discovery_rate_per_host)
    )
    classifier = build_classifier(settings, ledger=cost_ledger)
    tl = threading.local()
    created: list[object] = []
    lock = threading.Lock()

    def _components():
        if not hasattr(tl, "res"):
            tl.res = DomainResolver(settings, cost_ledger=cost_ledger)
            tl.enr = Enricher(settings, cost_ledger=cost_ledger)
            tl.exi = ExistenceVerifier(settings, registry_checker=registry_checker)
            tl.val = EmailValidator(settings, cost_ledger=cost_ledger)
            with lock:
                created.extend([tl.res, tl.enr, tl.exi, tl.val])
        return tl.res, tl.enr, tl.exi, tl.val

    def _work(dc: DiscoveredCompany):
        res, enr, exi, val = _components()
        try:
            found = res.resolve(dc)
        except Exception as exc:  # 해석 실패는 이번 배치에서만 스킵(다음 배치 재시도).
            log.info("resolve.backfill.error", key=dc.canonical_key, err=str(exc))
            return dc, None, None
        if not found:
            return dc, None, None
        dc2 = dc.model_copy(update={"domain": found})
        try:
            return dc2, found, _build_lead(
                dc2, enricher=enr, existence=exi, email_validator=val, classifier=classifier
            )
        except Exception as exc:  # 1건 실패가 배치를 안 죽이게(제약② 격리).
            log.info("fill.enrich.error", key=dc2.canonical_key, err=str(exc))
            return dc2, found, None

    processed = resolved = promoted = 0
    with sm() as ws:
        seen_domains = load_seen_domains(ws)  # 배치 시작 스냅샷 — 이 배치 내 중복도 아래서 누적.
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for dc, found, lead in pool.map(_work, targets):
                processed += 1
                if not found:
                    continue
                if backfill_domain(ws, dc.canonical_key, found):
                    resolved += 1
                ws.commit()  # 도메인 기록은 항상 남긴다(승격 실패해도 재시도 방지).
                rdom = normalize_domain(found)
                if rdom is not None and rdom in seen_domains:
                    # 이미 원장에 있는(또는 이번 배치에서 먼저 처리된) 회사와 동일 도메인
                    # → 별개 company 로 승격하지 않는다(제약① 중복방지, 흡수는 오프라인
                    # dedup-report/워크벤치가 후속 처리).
                    log.info("resolve.backfill.dedup_skip", key=dc.canonical_key, domain=rdom)
                    continue
                if rdom is not None:
                    seen_domains.add(rdom)
                if lead is not None:
                    _persist_lead(ws, dc, lead)
                    if lead.company.is_active:
                        promoted += 1

    for obj in created:
        close = getattr(obj, "close", None)
        if callable(close):
            close()
    return processed, resolved, promoted
