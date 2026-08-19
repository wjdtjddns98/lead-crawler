"""이메일 채우기 consumer — 큐에 쌓인 '실존·무이메일' 회사에 이메일을 배치 병렬로 채운다.

발견(producer)과 이메일 보강(consumer)을 분리한 파이프라인의 소비자 단계. 발견 크롤이
``discovery_only`` 로 빠르게 회사+홈페이지를 큐에 쌓으면, 이 소비자가 무이메일 회사를
배치로 잡아 헤드리스/OCR 까지 돌려 이메일을 채우고 DB 를 갱신한다(멱등 — 채워지면 대상에서
빠짐). 기존 파이프라인 함수(_build_lead/_persist_lead)를 그대로 재사용한다.

CLI ``leadcrawler fill-emails [--loop]`` 가 이 모듈을 구동한다.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import bindparam, text
from sqlalchemy.orm import sessionmaker

from ..config import Settings
from ..cost_ledger import CostLedger
from ..dedup import normalize_domain
from ..enrich.enricher import Enricher
from ..enrich.industry_classify import build_classifier
from ..logging import get_logger
from ..sources.base import DiscoveredCompany
from ..sources.countries import country_match_set
from ..sources.domain_resolver import DomainResolver
from ..sources.http import HostRateLimiters
from ..storage.repository import backfill_domain, load_seen_domains
from ..verify.email_validator import EmailValidator
from ..verify.existence import ExistenceVerifier
from ..verify.registry_active import build_registry_checker
from .run import _build_lead, _close_in_workers, _persist_lead

log = get_logger("pipeline.fill")

# 대상 = site_alive(실존)·도메인 보유·이메일 연락처 없음. discovered_company 를 조인해
# enrich 에 필요한 발견필드(도메인·등록처·업종 등)를 함께 가져온다. 채워지면 자동 이탈(멱등).
# {scope} = 국가 스코프 슬롯(아래 _scoped) — 크롤 companion 이 국가 명시선택 크롤이면 그
# 국가들로 제한한다(2026-07-27: KR 제외 크롤에 옛 KR 물량이 승격·큐 유입되던 사고). CLI
# 단독 백필·무선택(전체) 크롤은 빈 슬롯(전세계, 현행 유지).
_TARGET_SQL = """
    select d.canonical_key, d.name, d.country, d.industry, d.listed, d.domain,
           d.registry, d.registry_id, d.source, d.segment, d.reg_no, d.region,
           d.ticker, d.phone, d.ir_url, d.name_eng, d.address
    from company co
    join discovered_company d on d.canonical_key = co.canonical_key
    where co.site_alive is true
      and coalesce(d.domain, '') <> ''
      and not exists (
          select 1 from contact ct where ct.company_id = co.id and ct.type = 'email'
      ){scope}
    -- 오래 안 건드린 것부터(라운드로빈). 처리하면 save_lead→save_discovered 가
    -- last_crawled_at 을 현재시각으로 밀어 **대기열 뒤로 보내므로** 다음 배치가 다음
    -- 구간을 잡는다. 구 `order by co.id` 는 이메일을 못 찾은 회사가 대상에서 안 빠져
    -- 앞머리 200건만 무한 반복했다(2026-07-31 실측: 800건 처리에 대기열 9 감소).
    order by d.last_crawled_at asc, co.id
    limit :limit
    """
_COUNT_SQL = """
    select count(*) from company co
    join discovered_company d on d.canonical_key = co.canonical_key
    where co.site_alive is true and coalesce(d.domain, '') <> ''
      and not exists (select 1 from contact ct where ct.company_id = co.id and ct.type = 'email')
      {scope}
    """

_SCOPE_CLAUSE = "and lower(d.country) in :country_scope"

# 같은 도메인이 발견 원장에 이미 이만큼 있으면 기업 공식 도메인이 아니라 디렉터리/
# 플랫폼으로 판정한다(한 도메인을 N개 회사가 공유하는 건 구조적으로 불가능).
# 2026-08-10 사고: 해석기가 기업정보 디렉터리(nicebizinfo 등)를 오채택 → 원장에 무제한
# 기록 → promote 백필이 무차별 승격해 company 2만여 건이 오염됐다. blocklist(개별 나열)는
# 항상 뒤처지므로, 미지의 디렉터리도 여기서 구조적으로 끊는다.
_DOMAIN_OVERSHARE_CAP = 3


def _domain_overshared(session, domain: str) -> bool:  # noqa: ANN001 (Session)
    """이 도메인이 발견 원장에서 이미 과공유(디렉터리 신호)인지 — 기록·승격 금지 판정.

    ponytail: ① 등가비교는 정규화값 기준 — 해석기 출력(이 가드의 유입 경로)은 항상
    정규화 root 라 성립하고, 소스가 raw 표기로 넣은 소수는 언더카운트될 수 있다(허용 —
    업그레이드는 save_discovered 정규화 통일). ② 해석 성공 건당 COUNT 1회 — 배치≤200이라
    수용, 병목이 되면 배치 도메인 일괄 ANY 조회+인덱스로. ③ 정당한 계열사 도메인 공유가
    캡을 넘으면 그 행은 영구 miss 로 회전한다(오염 차단 우선 — 구제는 워크벤치 수동).
    """
    n = session.execute(
        text("select count(*) from discovered_company where domain = :d"), {"d": domain}
    ).scalar()
    return int(n or 0) >= _DOMAIN_OVERSHARE_CAP


def _scoped(
    sql_tpl: str,
    countries: Iterable[str] | None,
    *,
    exclude_industries: Iterable[str] | None = None,
    exclude_listed: bool = False,
):
    """SQL 템플릿의 ``{scope}`` 슬롯에 대상 필터를 조합 주입한다 — (stmt, params) 반환.

    선택 국가는 :func:`country_match_set` 별칭 집합으로 확장해 원장의 자유표기
    ('KR'/'대한민국' 혼재)를 같은 국가로 접는다. countries 가 비면 국가 필터 없음(전세계).
    ``exclude_industries`` 는 발견 원장의 정규 업종 라벨을 제외하고,
    ``exclude_listed`` 는 상장 확정(listed='listed')만 제외한다(unknown 은 대상 유지 —
    NPS 미스=비상장 경향이라 실질 비상장일 가능성이 높다).
    """
    clauses: list[str] = []
    params: dict[str, object] = {}
    binds = []
    match = country_match_set(countries) if countries else set()
    if match:
        clauses.append(_SCOPE_CLAUSE)
        params["country_scope"] = sorted(match)
        binds.append(bindparam("country_scope", expanding=True))
    if exclude_listed:
        clauses.append("and coalesce(d.listed, '') <> 'listed'")
    excl = sorted({i.strip().lower() for i in (exclude_industries or []) if i and i.strip()})
    if excl:
        # 정본 라벨 = co.industry(큐 필터·LLM 재분류 기준, 2026-08-18 리뷰 HIGH). 단
        # resolve 경로(미승격 — co left join null)와 co.industry 빈값은 발견 라벨
        # d.industry 로 폴백한다(가용한 최선). 소문자 정규화는 국가절과 동일 관례.
        clauses.append(
            "and lower(coalesce(nullif(co.industry, ''), d.industry, ''))"
            " not in :industry_excl"
        )
        params["industry_excl"] = excl
        binds.append(bindparam("industry_excl", expanding=True))
    stmt = text(sql_tpl.format(scope="".join(f" {c}" for c in clauses)))
    if binds:
        stmt = stmt.bindparams(*binds)
    return stmt, params


def _close_own(tl: threading.local) -> None:
    """현재 스레드의 스레드로컬 컴포넌트(enr/exi/val)를 닫는다 — 워커 스레드에서 호출."""
    for name in ("enr", "exi", "val"):
        obj = getattr(tl, name, None)
        if obj is not None:
            close = getattr(obj, "close", None)
            if callable(close):
                close()


def _dc_from_row(r) -> DiscoveredCompany:  # noqa: ANN001 (SQLAlchemy Row)
    return DiscoveredCompany(
        canonical_key=r.canonical_key, name=r.name, country=r.country or "",
        industry=r.industry or "", listed=r.listed or "unknown", domain=r.domain,
        registry=r.registry, registry_id=r.registry_id, source=r.source or "",
        segment=r.segment, reg_no=r.reg_no, region=r.region, ticker=r.ticker,
        phone=r.phone, ir_url=r.ir_url, name_eng=r.name_eng, address=r.address,
    )


# 정체 종료 코드 — bat 러너는 코드 무관 60s 후 재기동, supervisor 는 비정상종료 백오프
# 재기동(연속 3회 회로차단). 정상종료(0)와 구분되는 임의의 식별값.
_STALL_EXIT_CODE = 86


class _StallWatchdog:
    """배치 진행 정체 감시 — ``stall_s`` 동안 ``beat()`` 이 없으면 프로세스를 종료한다.

    2026-08-18 실사고: Playwright 드라이버 무응답으로 워커의 sync 호출이 영구 미반환 →
    ``pool.map`` 소비 루프가 그 future 를 무한 대기(로그·CPU 0, 4시간 정지). 파이썬은
    스레드를 강제 종료할 수 없으므로 **프로세스 종료가 유일하게 신뢰 가능한 복구**다 —
    CLI 는 러너(bat)/supervisor 가 재기동하고 배치는 멱등이라 유실 없이 이어받는다.
    ``stall_s`` 가 0/None 이면 완전 무장해제(웹서버 내 background fill 경로 — 서버를
    죽일 수 없으므로 종전 동작 유지).

    ponytail: 진행 판정 = 결과 소비 1건. 극단적으로 느린 정상 배치도 stall_s 를 넘기면
    종료되지만 피해 = 재기동 1회(멱등). 상한은 CLI ``--stall-exit-secs`` 로 조정.
    """

    def __init__(
        self,
        stage: str,
        stall_s: float | None,
        *,
        _exit=os._exit,
        _kill_children: bool | None = None,
    ) -> None:
        self._stage = stage
        self._stall_s = float(stall_s or 0)
        self._exit = _exit
        # 자식 트리 정리 여부 — 종료 방식(_exit)과 분리해 테스트 가능하게(리뷰 MED).
        self._kill_children = (
            (_exit is os._exit and sys.platform == "win32")
            if _kill_children is None
            else _kill_children
        )
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._watch, daemon=True, name=f"stall-watchdog-{stage}"
        )

    def beat(self) -> None:
        """진행 1건 보고 — 소비 루프가 결과를 받을 때마다 호출한다."""
        self._last = time.monotonic()

    def _watch(self) -> None:
        poll = min(10.0, max(0.05, self._stall_s / 4))
        while not self._stop.wait(poll):
            idle = time.monotonic() - self._last
            if idle >= self._stall_s:
                if self._stop.is_set():  # 정상 종료 직후 발화 방지 — 블록을 나왔으면 무장해제.
                    return
                log.info("fill.stall.exit", stage=self._stage, idle_s=round(idle))
                try:  # 러너가 stdout 을 로그파일로 리다이렉트 — os._exit 전 플러시 필수.
                    sys.stdout.flush()
                    sys.stderr.flush()
                except Exception:
                    pass
                if self._kill_children:
                    # bat 러너 경로엔 Job Object 가 없어 os._exit 만으론 Playwright
                    # node/Chromium 손자가 고아로 쌓인다(리뷰 HIGH — 정체 사이클마다
                    # 브라우저 트리 1개씩 누적 = 2026-07-31 OOM 재래). **자식 트리만**
                    # 죽이고 자신은 os._exit 로 나가 종료코드 86(정체 식별)을 보존한다.
                    # 실패는 전부 로그로 가시화(무음이면 고아 누적이 '정상 종료'로 보임 —
                    # 리뷰 MED). ponytail: PID 스캔은 재사용 오폭의 이론적 여지가 있는
                    # 임시방편 — 근본은 CLI 자기 Job Object 결속(winjob), 별도 트랙.
                    import subprocess

                    try:
                        proc = subprocess.run(
                            [
                                "powershell", "-NoProfile", "-Command",
                                "(Get-CimInstance Win32_Process -Filter"
                                f" 'ParentProcessId={os.getpid()}'"
                                " | Where-Object { $_.ProcessId -ne $PID }).ProcessId",
                            ],
                            capture_output=True, text=True, timeout=30, check=False,
                        )
                        pids = [p.strip() for p in proc.stdout.split() if p.strip().isdigit()]
                        if not pids:  # 자식 0 vs 열거 실패(권한 거부 등) 구분용.
                            log.info(
                                "fill.stall.childkill.none",
                                stage=self._stage, stderr=proc.stderr.strip()[:200],
                            )
                        for pid in pids:
                            try:
                                subprocess.run(
                                    ["taskkill", "/T", "/F", "/PID", pid],
                                    capture_output=True, timeout=30, check=False,
                                )
                            except Exception as exc:  # 1건 실패는 다음 자식 계속.
                                log.info(
                                    "fill.stall.childkill.error", pid=pid, err=str(exc)
                                )
                    except Exception as exc:
                        log.info("fill.stall.childkill.error", err=str(exc))
                self._exit(_STALL_EXIT_CODE)
                return  # 테스트 주입 _exit 는 반환한다 — 중복 발화 방지.

    def __enter__(self) -> _StallWatchdog:
        if self._stall_s > 0:
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread.ident is not None:  # 기동된 경우만 — set() 이 wait 를 즉시 깨운다.
            self._thread.join(timeout=15.0)


def _stamp_attempt(sm: sessionmaker, keys: list[str]) -> None:
    """배치 대상 전체의 ``last_crawled_at`` 을 **시작 전에** 스탬프한다(시도 기록).

    대상 SQL 이 ``last_crawled_at asc`` 정렬이라, 처리 실패(예외·정체 종료 포함)로
    스탬프가 안 남으면 같은 행이 영원히 선두에 온다 — 특정 행이 Playwright 를 얼리는
    poison row 면 "무한 정지"가 "무한 재기동"으로 바뀔 뿐 전진이 0 이 된다(리뷰 HIGH).
    선스탬프하면 어떤 경로로 죽어도 재기동이 다음 구간을 잡고, 실패 행은 한 바퀴 뒤
    재시도로 복귀한다. 성공 행은 처리 시 save 경로가 다시 스탬프하므로 의미 손실 없음.
    """
    with sm() as s:
        s.execute(
            text(
                "update discovered_company set last_crawled_at = :now"
                " where canonical_key in :keys"
            ).bindparams(bindparam("keys", expanding=True)),
            {"now": datetime.now(timezone.utc), "keys": keys},
        )
        s.commit()


def count_targets(
    sm: sessionmaker,
    countries: Iterable[str] | None = None,
    *,
    exclude_industries: Iterable[str] | None = None,
    exclude_listed: bool = False,
) -> int:
    """현재 이메일 채우기 대상(실존·무이메일) 회사 수(``_scoped`` 필터 적용 후)."""
    stmt, params = _scoped(
        _COUNT_SQL, countries, exclude_industries=exclude_industries, exclude_listed=exclude_listed
    )
    with sm() as s:
        return int(s.execute(stmt, params).scalar() or 0)


def fill_batch(
    settings: Settings, sm: sessionmaker, *, limit: int, workers: int,
    countries: Iterable[str] | None = None,
    exclude_industries: Iterable[str] | None = None,
    exclude_listed: bool = False,
    stall_exit_s: float | None = None,
) -> tuple[int, int]:
    """대상 최대 ``limit`` 개를 ``workers`` 병렬로 enrich 해 이메일을 채운다.

    반환 (처리수, 신규이메일수). enrich(_build_lead)는 워커스레드에서, DB 적재(_persist_lead)는
    메인스레드 단독(파이프라인 계약). 1건 실패는 격리(배치 전체 보호). 멱등이라 재호출 안전.
    ``countries`` 지정 시(크롤 companion — 국가 명시선택 크롤) 그 국가 회사만 채운다.
    ``exclude_industries``/``exclude_listed`` 는 CLI 백필의 대상 제외 필터(``_scoped`` 참고).
    ``stall_exit_s`` 를 주면 그 초 동안 진행이 없을 때 프로세스를 종료한다(CLI 전용 —
    :class:`_StallWatchdog` 참고. 웹서버 내 호출자는 절대 주지 말 것).
    """
    stmt, params = _scoped(
        _TARGET_SQL, countries, exclude_industries=exclude_industries, exclude_listed=exclude_listed
    )
    with sm() as rd:
        targets = [_dc_from_row(r) for r in rd.execute(stmt, {**params, "limit": limit}).all()]
    if not targets:
        return 0, 0
    _stamp_attempt(sm, [t.canonical_key for t in targets])  # 선스탬프 — 재기동 전진 보장.

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
    with _StallWatchdog("fill", stall_exit_s) as wd, sm() as ws:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for dc, lead in pool.map(_work, targets):
                wd.beat()
                processed += 1
                if lead is not None:
                    _persist_lead(ws, dc, lead)
                    if lead.email is not None:
                        emails += 1
            # Playwright 보유 컴포넌트는 만든 워커 스레드만 닫을 수 있다(메인스레드 close 는
            # greenlet.error 로 조용히 no-op → 배치마다 브라우저 누수 = 2026-07-31 OOM 뿌리).
            # close 행도 감시 범위 안(watchdog 은 이 블록 전체를 덮는다).
            _close_in_workers(pool, lambda: _close_own(tl))

    for obj in created:
        close = getattr(obj, "close", None)
        if callable(close):
            close()
    return processed, emails


# 대상 = 도메인 미보유 + 미승격(company 행 없음) 발견 행. 기본은 **국가 무관**(전세계 —
# 이 프로젝트는 전 산업·전 국가 IR 연락처 추출이 목적)이되, ``countries`` 를 넘기면
# ``{scope}`` 로 그 국가들만 되짚는다(2026-07-27: KR 제외 크롤에 옛 KR 물량이 승격·큐
# 유입되던 사고 — 원장 정렬이 last_crawled_at desc 라 직전 KR 크롤 물량이 맨 앞에 왔다.
# 당시 크롤 병행 companion 은 이후 제거됐고, 현 유일 호출자인 CLI
# ``backfill-resolve-domains`` 는 전세계로만 호출 — 스코프는 향후 CLI 옵션 노출용).
# 최초 발견 때 도메인을 못 준 소스(GLEIF·NPS 등)가 dedup 시드로 영원히 정체되는
# 사각(위 backfill_domain 독스트링 참고)을 최근 발견 우선으로 재시도한다.
_RESOLVE_TARGET_SQL = """
    select d.canonical_key, d.name, d.country, d.industry, d.listed, d.domain,
           d.registry, d.registry_id, d.source, d.segment, d.reg_no, d.region,
           d.ticker, d.phone, d.ir_url, d.name_eng, d.address
    from discovered_company d
    left join company co on co.canonical_key = d.canonical_key
    where coalesce(d.domain, '') = '' and co.id is null
      {scope}
    order by d.last_crawled_at asc, d.canonical_key
    limit :limit
    """
_RESOLVE_COUNT_SQL = """
    select count(*) from discovered_company d
    left join company co on co.canonical_key = d.canonical_key
    where coalesce(d.domain, '') = '' and co.id is null
      {scope}
    """


def count_resolve_targets(
    sm: sessionmaker,
    countries: Iterable[str] | None = None,
    *,
    exclude_industries: Iterable[str] | None = None,
    exclude_listed: bool = False,
) -> int:
    """현재 도메인 해석 대상(도메인없음·미승격) 회사 수(``_scoped`` 필터 적용 후)."""
    stmt, params = _scoped(
        _RESOLVE_COUNT_SQL, countries,
        exclude_industries=exclude_industries, exclude_listed=exclude_listed,
    )
    with sm() as s:
        return int(s.execute(stmt, params).scalar() or 0)


def resolve_batch(
    settings: Settings, sm: sessionmaker, *, limit: int, workers: int,
    countries: Iterable[str] | None = None,
    exclude_industries: Iterable[str] | None = None,
    exclude_listed: bool = False,
    stall_exit_s: float | None = None,
) -> tuple[int, int, int]:
    """대상 최대 ``limit`` 개의 도메인을 해석해 채우고, 실존이면 승격까지 시도한다.

    기본(``countries=None``)은 전세계, ``countries`` 지정 시 그 국가 발견행만 되짚는다
    (비선택 국가가 승격돼 큐에 섞이는 것 방지 — 현 유일 호출자 CLI 는 전세계로만 호출).

    반환 (처리수, 도메인해석수, 신규승격수). enrich/existence/validate 는 워커별 독립
    인스턴스로 병렬화하지만(``fill_batch`` 와 동일 스레드로컬 관례), **도메인 해석기는
    전 워커가 공유하는 단일 인스턴스**를 쓴다 — ``DomainResolver`` 의 캡
    (``domain_resolve_max``)이 워커별로 나뉘면 워커수배로 새어나가던 결함을 막는다
    (2026-07-10 적대 리뷰 MED-1). 캡 체크는 ``DomainResolver`` 내부 락으로 원자화돼
    있어(스레드 안전) 공유해도 실제 네트워크 호출은 병렬로 나간다.
    ``resolve_batch`` 호출마다 새 인스턴스를 만들므로 이 캡은 **배치당** 상한이지
    "런"(CLI ``--loop`` 는 배치를 계속 반복 호출) 전체 상한은 아니다 —
    무료 CSE(100/일)는 큰 문제 없고, 유료 Serper 는 별도 ``cost_budget_enforce``/
    ``monthly_budget_krw`` 예산가드가 실제 과금 상한이다(``domain_resolve_max`` 는
    배치 단위 페이싱 목적, 예산 하드캡이 아님). 진짜 런/일 단위 상한이 필요해지면
    호출자가 ``DomainResolver`` 를 만들어 여러 배치에 걸쳐 재사용하도록 승격.
    **도메인 동치 dedup(제약①)은 메인스레드에서 순차 판정**한다 — 워커가 해석한 도메인이
    이번 배치 안에서 서로 겹치거나(다른 표기의 같은 회사) 이미 원장에 있는 도메인과
    겹치면(``load_seen_domains`` 스냅샷) 승격을 건너뛰어 중복 ``company`` 행을 막는다.
    도메인은 **해석까지 성공한 경우에만** 기록한다 — 승격은 실패해도(실존탈락·동치스킵)
    기록해 재시도를 막지만, 그 뒤 enrich/existence 가 예외를 던진 경우는 기록하지 않는다
    (다음 배치가 재시도 — 적대 리뷰 HIGH-MED: 일시적 크래시로 도메인만 기록되고 승격
    기회가 영구 소멸하던 결함).
    """
    stmt, params = _scoped(
        _RESOLVE_TARGET_SQL, countries,
        exclude_industries=exclude_industries, exclude_listed=exclude_listed,
    )
    with sm() as rd:
        targets = [_dc_from_row(r) for r in rd.execute(stmt, {**params, "limit": limit}).all()]
    if not targets:
        return 0, 0, 0
    _stamp_attempt(sm, [t.canonical_key for t in targets])  # 선스탬프 — 재기동 전진 보장.

    cost_ledger = CostLedger(settings, persist=True)
    registry_checker = build_registry_checker(
        settings, rate_limiters=HostRateLimiters(default_rate=settings.discovery_rate_per_host)
    )
    classifier = build_classifier(settings, ledger=cost_ledger)
    # 해석기용 공유 호스트 레이트리미터 — 워커가 provider(=Fetcher)를 공유하는데(아래)
    # 없으면 openapi.naver.com/google.serper.dev 를 워커 각자 min_interval 로 racy 하게
    # 때린다(2026-07-10 적대 리뷰 MED-3).
    resolve_rate_limiters = HostRateLimiters(default_rate=settings.discovery_rate_per_host)
    shared_resolver = DomainResolver(
        settings, cost_ledger=cost_ledger, rate_limiters=resolve_rate_limiters
    )  # 전 워커 공유(캡 원자적).
    tl = threading.local()
    created: list[object] = [shared_resolver]
    lock = threading.Lock()

    def _components():
        if not hasattr(tl, "enr"):
            tl.enr = Enricher(settings, cost_ledger=cost_ledger)
            tl.exi = ExistenceVerifier(settings, registry_checker=registry_checker)
            tl.val = EmailValidator(settings, cost_ledger=cost_ledger)
            with lock:
                created.extend([tl.enr, tl.exi, tl.val])
        return shared_resolver, tl.enr, tl.exi, tl.val

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
        except Exception as exc:
            # enrich/existence 예외 — found=None 으로 반환해 도메인을 **기록하지 않는다**.
            # 해석 자체는 성공했지만 그 사실을 남기면 이 행이 대상 SQL(domain='')에서
            # 영구 이탈해 재시도 기회를 잃는다(승격도 못 하고 fill-emails 대상도 아님 —
            # 2026-07-10 적대 리뷰 HIGH-MED, 일시적 enrich 크래시로 영구 정체 실증됨).
            # fill_batch 의 기존 관례(예외=이번 라운드 미스)와 동일한 안전한 방향(제약②).
            log.info("fill.enrich.error", key=dc2.canonical_key, err=str(exc))
            return dc2, None, None

    processed = resolved = promoted = 0
    with _StallWatchdog("resolve", stall_exit_s) as wd, sm() as ws:
        seen_domains = load_seen_domains(ws)  # 배치 시작 스냅샷 — 이 배치 내 중복도 아래서 누적.
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            # 해석 실패(miss)는 별도 스탬프가 필요 없다 — 배치 시작 시 _stamp_attempt 가
            # 대상 전체를 이미 뒤로 보냈다(구 missed 일괄 update 는 정체 종료 시 통째로
            # 유실돼 같은 창을 재과금 재시도하던 결함 — 리뷰 MED).
            for dc, found, lead in pool.map(_work, targets):
                wd.beat()
                processed += 1
                if not found:
                    continue
                rdom = normalize_domain(found)
                if rdom is not None and _domain_overshared(ws, rdom):
                    # 과공유 도메인(디렉터리 신호) — 기록도 승격도 하지 않는다(위
                    # _DOMAIN_OVERSHARE_CAP 주석 참고). 선스탬프가 이미 뒤로 보냈다.
                    log.info("resolve.backfill.overshared", key=dc.canonical_key, domain=rdom)
                    continue
                if backfill_domain(ws, dc.canonical_key, found):
                    resolved += 1
                ws.commit()  # 도메인 기록은 항상 남긴다(승격 실패해도 재시도 방지).
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
            # fill_batch 와 동일 — Playwright 보유 컴포넌트는 워커 스레드 자신이 닫는다.
            _close_in_workers(pool, lambda: _close_own(tl))

    for obj in created:
        close = getattr(obj, "close", None)
        if callable(close):
            close()
    return processed, resolved, promoted
