"""도메인 보유 미승격 발견행 승격 — 트랙 S(세그먼트 잡)·백필 스크립트 공용 코어.

대상 = ``discovered_company`` 중 **도메인은 있는데** ``company`` 행이 없는 회사(발견 당시
실존검증까지 못 갔거나 검증에서 떨어진 뒤 재시도가 없던 사각). 도메인이 이미 있으므로
검색 API 를 전혀 안 쓴다 — 자기 사이트 HTTP 왕복만 든다.

기존 파이프라인 함수(``_build_lead``/``_persist_lead``)를 그대로 재사용 — 제약②(실존만
저장)는 ``_persist_lead`` 가 그대로 강제하므로, 죽은 사이트는 여기서도 승격되지 않고
원장만 갱신된다. 멱등: 승격되면 다음 배치의 대상 쿼리에서 자동 제외된다(``co.id is null``).

원래 ``scripts/backfill_promote_domained.py`` 단독 스크립트였던 로직을 세그먼트 작업 큐
설계(``docs/segment-jobs-design.md`` §4)에 따라 이관 — 스크립트는 이제 이 모듈을 호출하는
얇은 CLI 래퍼다. 배치 1회분을 처리하는 :func:`promote_batch` 를 호출자(스크립트 또는
향후 ``segment-run`` 자식 CLI)가 반복 호출해 전체 대상을 소화한다.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from ..config import Settings
from ..dedup import normalize_domain
from ..enrich.enricher import Enricher
from ..logging import get_logger
from ..sources.base import DiscoveredCompany
from ..verify.email_validator import EmailValidator
from ..verify.existence import ExistenceVerifier
from .fill import (  # PromoteRun 은 fill/resolve/promote 공용 — 여기서 재수출(기존 import 경로 유지).
    _DOMAIN_OVERSHARE_CAP,
    PromoteRun,
    _StallWatchdog,
    _close_own,
    _dc_from_row,
    _scoped,
)
from .run import _build_lead, _close_in_workers, _persist_lead

log = get_logger("pipeline.promote")

# canonical_key 커서로 전진한다. "대상에서 빠지는가"에 기대면 안 된다 — 죽은 사이트는
# 제약②로 company 가 안 생겨 co.id is null 을 계속 만족하므로, 같은 배치가 영원히 재선택
# 된다. discovered_company 의 PK 는 canonical_key 다(id 컬럼 없음 — 2026-07-31 실사고).
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
_COUNT_SQL = """
    select count(*)
    from discovered_company d
    left join company co on co.canonical_key = d.canonical_key
    where co.id is null and coalesce(d.domain, '') <> '' {scope}
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


def _split_multi(vals: list[str] | None) -> list[str] | None:
    """반복 지정 + 쉼표 병기 옵션을 평탄화한다(CLI --exclude-industry 와 동일 관례)."""
    out = [t.strip() for v in (vals or []) for t in v.split(",") if t.strip()]
    return out or None


def _listed_filter_flags(listed: str) -> tuple[bool, bool]:
    """tri-state ``listed``(설계 §4/§5) 를 ``_scoped`` 의 (exclude_listed, only_listed) 로.

    미지의 값은 즉시 거부한다 — 조용히 "무필터"로 흡수하면 오타 하나로 전 우주 스캔이 돈다
    (``_scoped`` 가 모순 조합을 ValueError 로 거부하는 방침과 대칭). PR3 부터 이 값은 DB
    (``backfill_job.listed``)에서 온다.
    """
    if listed == "listed":
        return False, True
    if listed == "unlisted":
        return True, False
    if listed == "unknown":
        return False, False  # 무필터(기존 API 의미).
    raise ValueError(f"허용되지 않은 listed: {listed!r} (unknown|listed|unlisted)")


def count_promote_targets(
    sm: sessionmaker,
    countries: Iterable[str] | None = None,
    *,
    industries: Iterable[str] | None = None,
    exclude_industries: Iterable[str] | None = None,
    listed: str = "unknown",
    regions: Iterable[str] | None = None,
) -> int:
    """현재 승격 대상(도메인 보유·미승격) 회사 수(``_scoped`` 필터 적용 후)."""
    exclude_listed, only_listed = _listed_filter_flags(listed)
    stmt, params = _scoped(
        _COUNT_SQL, countries, industries=industries, exclude_industries=exclude_industries,
        exclude_listed=exclude_listed, only_listed=only_listed, regions=regions,
    )
    with sm() as s:
        return int(s.execute(stmt, params).scalar() or 0)


def promote_batch(
    settings: Settings,
    sm: sessionmaker,
    *,
    run: PromoteRun,
    after: str,
    limit: int,
    workers: int,
    guards: tuple[set[str], set[str]],
    countries: Iterable[str] | None = None,
    industries: Iterable[str] | None = None,
    exclude_industries: Iterable[str] | None = None,
    listed: str = "unknown",
    regions: Iterable[str] | None = None,
    stall_exit_s: float | None = None,
) -> tuple[int, str, int, int, int]:
    """대상 최대 ``limit`` 개를 승격 시도한다 — 배치 1회분(호출자가 반복 호출해 소화).

    반환 ``(rows, last_key, promoted, emails, failed)``. ``rows`` 는 이번 배치가 훑은
    행 수(0 이면 더 이상 대상이 없다 — ``last_key`` 는 이때 입력 ``after`` 그대로 미전진).
    커서(``last_key``)는 이 배치의 persist 가 전부 끝난 뒤의 값이므로, 호출자는 반환값을
    받은 **이후에만** 영속 커서에 기록해야 한다(선기록이면 미처리 행이 영구 스킵 — 리뷰 HIGH
    선례). 회사 1건 실패(enrich 예외)는 격리한다 — ``failed`` 만 +1 되고 커서는 그대로
    전진(예외로 배치 전체를 죽이지 않는다). ``_persist_lead`` 자체도 DB 예외를 회사 1건
    단위로 흡수하므로 이중 방어. 도메인 가드로 전량 스킵된 배치도 ``rows>0``·커서 전진
    (호출자 루프가 멈추지 않게).

    필터는 :func:`count_promote_targets` 와 같은 kwargs(``listed`` 는 "unknown"|"listed"|
    "unlisted"). ``run`` 은 런당 1회 :meth:`PromoteRun.open` 으로 만든 공유 컴포넌트.
    ``guards`` = (점유 도메인, 과공유 도메인) — 호출자가 세대(런)당 1회
    :func:`_load_domain_guards` 로 시드하고 배치 간 재사용한다. ``taken`` 은 이 함수가
    같은 런 안에서 신규 승격한 도메인을 계속 누적한다(호출자 소유 set 를 직접 mutate).

    워커 풀·Playwright 컴포넌트는 **배치마다** 만들고 워커 스레드 안에서 닫는다 —
    ``fill.fill_batch`` 와 같은 패턴(메인스레드 close 는 greenlet no-op 이라 누수, 2026-07-31
    OOM 근본수정). 배치당 브라우저 재기동 비용은 배치 크기로 상각한다(운영 --batch 100~200).
    """
    exclude_listed, only_listed = _listed_filter_flags(listed)
    stmt, params = _scoped(
        _TARGET_SQL, countries, industries=industries, exclude_industries=exclude_industries,
        exclude_listed=exclude_listed, only_listed=only_listed, regions=regions,
    )
    run.cost_ledger.refresh()  # 배치마다 월예산 캐시 재시드(다른 러너 과금 반영 — 리뷰 HIGH).
    cost_ledger, registry_checker, classifier = (
        run.cost_ledger, run.registry_checker, run.classifier
    )

    # 워커별 독립 인스턴스(공유 throttle 경쟁 회피) — fill.fill_batch 패턴 미러링.
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
            log.info("promote.enrich.error", key=dc.canonical_key, err=str(exc))
            return dc, None

    promoted = emails = failed = 0
    try:
        # 워치독은 대상 SQL 조회부터 감싼다 — DB 락/느린 쿼리 정체도 stall-exit 대상(리뷰 MED).
        with _StallWatchdog("promote", stall_exit_s) as wd:
            with sm() as rd:
                rows = rd.execute(stmt, {**params, "limit": limit, "after": after}).all()
            if not rows:
                return 0, after, 0, 0, 0
            last_key = rows[-1].canonical_key  # 다음 배치는 이 키 뒤부터(대상 0 건이어도 전진).

            taken, overshared = guards
            targets: list[DiscoveredCompany] = []
            for r in rows:
                dom = normalize_domain(r.domain)
                if dom is None or dom in taken or dom in overshared:
                    continue  # 이미 점유/과공유 도메인 — enrich 비용도 쓰지 않는다.
                taken.add(dom)  # 같은 런 안의 후속 중복(배치 내 포함)도 차단.
                targets.append(_dc_from_row(r))

            with sm() as ws, ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                try:
                    for dc, lead in pool.map(_work, targets):
                        wd.beat()  # 진행 보고 — 정체 판정 리셋.
                        if lead is None:
                            failed += 1
                            continue
                        if not _persist_lead(ws, dc, lead):
                            failed += 1  # 저장 실패 — 성공으로 집계하지 않는다(커서는 전진).
                            continue
                        # 제약②: 실존(active)만 company 로 저장된다 — 승격 여부는 그걸로 센다.
                        if lead.company.is_active:
                            promoted += 1
                            if lead.email is not None:  # 연락처는 실존 저장분에만 존재.
                                emails += 1
                finally:
                    # Playwright 보유 컴포넌트는 만든 워커 스레드만 닫을 수 있다(메인스레드
                    # close 는 greenlet.error 로 조용히 no-op → 브라우저 누수, 2026-07-31 OOM
                    # 뿌리). 예외 경로에서도 반드시 워커 안에서 닫는다.
                    _close_in_workers(pool, lambda: _close_own(tl))
    finally:
        for obj in created:
            close = getattr(obj, "close", None)
            if callable(close):
                close()
    return len(rows), last_key, promoted, emails, failed
