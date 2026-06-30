"""파이프라인 본체 — 발견부터 CompanyLead 까지.

제약 ①(중복) : ``seen``(canonical_key) + ``seen_domains``(정규화 도메인) 집합으로 이미
본 기업을 스킵 — 같은 기업이 reg:/dom: 등 다른 key 로 잡혀도 도메인 동치로 한 번만 추출.
제약 ②(실존) : ExistenceVerifier 로 죽은 기업을 거른다(검증 큐 대상).
dry_run 에서는 모든 단계가 네트워크 없이 결정적으로 동작한다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..cost_ledger import CostLedger
from ..emailrules import accepted_emails
from ..enrich.enricher import Enricher
from ..logging import get_logger
from ..models import (
    Company,
    CompanyLead,
    ContactType,
    EmailValidation,
    Listed,
)
from ..dedup import normalize_domain
from ..dedup_resolve.inline import find_inline_duplicate
from ..dedup_resolve.inline_lexical import InlineLexicalMatcher
from ..sources.base import DiscoveredCompany, Segment
from ..sources.domain_resolver import DomainResolver
from ..sources.registry import build_sources, close_sources, discover_segment
from ..storage.db import get_sessionmaker
from ..storage.repository import (
    load_seen_domains,
    load_seen_keys,
    save_discovered,
    save_lead,
)
from ..verify.email_validator import EmailValidator
from ..verify.existence import ExistenceVerifier
from ..verify.registry_active import build_registry_checker

log = get_logger("pipeline")

# 진행현황 콜백 시그니처 — 카운터 dict 를 받는다(웹 직접 크롤의 실시간 표시·DB 적재용).
# 키: segments_total·segments_done·discovered(중복제외 발견)·enriched(보강완료)·saved(실존저장).
ProgressCallback = Callable[[dict[str, int]], None]


def _listed_of(dc: DiscoveredCompany) -> Listed:
    """발견 단계 상장정보 문자열을 :class:`Listed` 로 안전 변환(미상 fallback)."""
    try:
        return Listed(dc.listed)
    except ValueError:
        return Listed.UNKNOWN


def _build_lead(
    dc: DiscoveredCompany,
    *,
    enricher: Enricher,
    existence: ExistenceVerifier,
    email_validator: EmailValidator,
) -> CompanyLead:
    """기업 1건: 연락처 보강 + 실존 검증 + 이메일 검증 → CompanyLead.

    seen·progress·leads·파이프라인 DB 세션에는 접근하지 않는다(그건 메인 스레드 전담).
    단, 라이브에서 enrich/validate 는 주입된 **cost_ledger 를 공유**해 record 하고(persist
    모드면 cost_ledger 테이블에 자체 짧은 세션으로 기록), 이 부분의 스레드안전은
    ``CostLedger`` 내부 락에 의존한다 — 워커가 '순수'해서가 아니다. 즉 워커 간 공유 가변
    상태는 cost_ledger 하나뿐이고, lead/company 테이블 적재만 메인 스레드 단독이다.
    """
    contacts = enricher.enrich(dc)
    candidates = accepted_emails(contacts)
    email = candidates[0] if candidates else None
    phone = next((c for c in contacts if c.type is ContactType.PHONE), None)
    form = next((c for c in contacts if c.type is ContactType.FORM), None)
    # enrich 가 이미 받은 home 생존신호를 넘겨 실존검증의 중복 HTTP 왕복을 없앤다(architect C).
    # 헤드리스로 렌더한 home 도 넘겨, verify_headless 가 같은 도메인을 또 렌더하지 않게 한다.
    ex = existence.verify(
        dc.domain,
        registry=dc.registry,
        registry_id=dc.registry_id,
        home_html=enricher.last_home_html,
        rendered_html=enricher.last_home_rendered_html,
    )
    company = Company(
        canonical_key=dc.canonical_key,
        name=dc.name,
        country=dc.country,
        industry=dc.industry,
        listed=_listed_of(dc),
        homepage=f"https://{dc.domain}" if dc.domain else None,
        domain=dc.domain,
        segment=dc.segment,
        is_active=ex.is_active,
        existence_confidence=ex.confidence,
        site_alive=ex.site_alive,
    )
    # 후보별 검증(MX/도메인/SMTP·딜리버러빌리티) — 선택 UI 에 신호 제공. validate_all_candidates
    # 가 꺼지면 선택 이메일(candidates[0])만 심층검증(SMTP/유료)하고 나머지는 형식/MX 까지만 —
    # 후보 수만큼 곱해지던 SMTP 핸드셰이크·유료 호출을 줄인다(산출의 선택 이메일 신호는 동일).
    deep_all = email_validator.settings.validate_all_candidates
    validations = {
        c.value: email_validator.validate(
            c.value, dc.domain, deep=deep_all or (email is not None and c.value == email.value)
        )
        for c in candidates
    }
    validation = (
        validations.get(email.value, EmailValidation()) if email else EmailValidation()
    )
    return CompanyLead(
        company=company,
        email=email,
        email_candidates=candidates,
        phone=phone,
        form=form,
        email_validation=validation,
        email_validations=validations,
    )


def run_pipeline(
    segments: Iterable[Segment],
    *,
    seen: set[str] | None = None,
    settings: Settings | None = None,
    persist: bool = False,
    on_progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
    target_saved: int | None = None,
) -> list[CompanyLead]:
    """세그먼트들을 처리해 검증된 :class:`CompanyLead` 목록을 반환한다.

    ``persist=True`` 면 DB 세션을 열어 ① 발견 원장(discovered_company)에 모든 신규
    기업을 기록(죽은 기업도 — 제약 ① 재추출 방지)하고, ② 실존(active) 기업만 회사·
    연락처 테이블에 저장한다(제약 ②). 기존 ``seen`` 은 원장 key 와 합쳐 dedup 시드가 된다.

    ``on_progress`` 가 주어지면 발견/보강/저장·세그먼트 진행 카운터 dict 를 단계마다
    호출한다(웹 직접 크롤의 실시간 현황). ``should_cancel`` 이 매 기업 처리 직전 True 를
    반환하면 협조적으로 중단한다(이미 처리된 결과는 보존). 둘 다 None 이면 기존 동작 그대로.

    ``target_saved`` 가 주어지면 실존 저장(``saved``) 누계가 그 값에 도달하는 즉시 세그먼트를
    더 돌지 않고 조기 종료한다("정해진 양만큼 뽑고 멈춤"). None(기본)이면 주어진 세그먼트를
    전부 깊게 소진한다(소스당 ``discovery_max_per_source`` 까지). 어느 경우든 dedup(제약①)으로
    이미 본 기업은 건너뛰므로, 같은 스코프 재크롤이 아니라 **새 기업을 계속 발견**할 때 채워진다.

    조기종료는 배치(flush) 경계에서만 평가하므로 정확히 ``target_saved`` 에서 멈추지 않고
    최대 ``batch_size``(병렬 시 ``workers*4``, 순차 시 1)만큼 오버슈트할 수 있다 — 마지막
    배치가 통째로 처리·적재된 뒤 카운터를 확인하기 때문. 상한이 아니라 하한 보장이다
    (``saved >= target_saved``).
    """
    settings = settings or get_settings()
    seg_list = list(segments)
    progress = {
        "segments_total": len(seg_list),
        "segments_done": 0,
        "discovered": 0,
        "enriched": 0,
        "saved": 0,
    }

    def _emit() -> None:
        if on_progress is not None:
            on_progress(dict(progress))

    def _target_hit() -> bool:
        """목표 실존 저장수(target_saved)에 도달했는지 — 도달 시 세그먼트 순회를 조기 종료."""
        return target_saved is not None and progress["saved"] >= target_saved
    seen = seen if seen is not None else set()
    # 도메인 동치 dedup(제약 ①) — 같은 기업이 등록처 key(reg:)와 검색 key(dom:)로 다르게
    # 잡혀도 정규화 도메인이 같으면 한 번만 추출한다. seen(키)과 짝을 이뤄 런 전체·DB 영속을
    # 가로질러 적용된다(within-segment 머지는 discover_segment 가 1차로 수행).
    seen_domains: set[str] = set()
    # 라이브에서만 등록처 active 체커 주입(키 있을 때) — 실존 판정의 최강 신호(active=0.9 우선).
    # dry_run 은 도메인 유무로 결정적이라 미주입.
    registry_checker = None if settings.dry_run else build_registry_checker(settings)
    existence = ExistenceVerifier(settings, registry_checker=registry_checker)
    # 라이브에서만 과금 원장을 켠다(dry_run 은 유료 호출이 없음). persist 면 DB 에 누계
    # 적재(월·다중런 합산), 아니면 인메모리(현재 런 내 가드만). 예산 초과 시 유료 차단.
    cost_ledger = CostLedger(settings, persist=persist) if not settings.dry_run else None
    email_validator = EmailValidator(settings, cost_ledger=cost_ledger)
    enricher = Enricher(settings, cost_ledger=cost_ledger)
    # 도메인 해석(opt-in·라이브) — 발견이 도메인을 못 준 기업(GLEIF 등)을 회사명으로 보강.
    # 없으면 enrich 가 즉시 빈손이라 사이트·이메일을 못 얻는다(핵심 커버리지 갭 해소).
    resolver = (
        DomainResolver(settings, cost_ledger=cost_ledger)
        if settings.resolve_domains and not settings.dry_run
        else None
    )

    # 기업 단위 병렬 추출 — enrich/verify/validate 는 I/O 바운드라 동시 처리로 처리량을 올린다.
    # 워커>1 이면 ThreadPool + 워커별 독립 인스턴스(공유 throttle 경쟁 회피). dedup·카운터·DB
    # 적재는 메인 스레드 전담. pool.map 순서보존 + _build_lead 결정성 → workers 무관 산출 동일.
    workers = settings.enrich_workers
    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    _tl = threading.local()
    _created: list[object] = []  # 워커별 생성 인스턴스(종료 시 close).
    _created_lock = threading.Lock()

    def _process_one(dc: DiscoveredCompany) -> CompanyLead:
        if pool is None:  # 순차 — 공유 인스턴스(메인 스레드 단독 실행, 기존 경로와 동일).
            return _build_lead(
                dc, enricher=enricher, existence=existence, email_validator=email_validator
            )
        w = getattr(_tl, "trio", None)
        if w is None:  # 워커 스레드당 독립 인스턴스 1회 생성. registry_checker 까지 워커별로
            # 따로 만들어 공유 Fetcher 의 throttle(self._last) 경쟁을 없앤다(아키텍트 MAJOR).
            rc = None if settings.dry_run else build_registry_checker(settings)
            enr = Enricher(settings, cost_ledger=cost_ledger)
            exi = ExistenceVerifier(settings, registry_checker=rc)
            val = EmailValidator(settings, cost_ledger=cost_ledger)
            w = (enr, exi, val)
            _tl.trio = w
            with _created_lock:
                _created.extend([enr, exi, val, *([rc] if rc is not None else [])])
        enr, exi, val = w
        return _build_lead(dc, enricher=enr, existence=exi, email_validator=val)

    leads: list[CompanyLead] = []
    pending: list[DiscoveredCompany] = []  # 배치 — 메인 dedup 통과분, 풀로 동시 처리.
    # workers==1(pool None)이면 배치 1 — 기업별 즉시 처리·적재로 기존 순차 동작과 동일
    # (진행카운터 타이밍·실패 시 직전까지 보존). 병렬이면 workers*4 로 모은다.
    batch_size = max(1, workers * 4) if pool is not None else 1

    def _flush() -> None:
        """대기 배치를 (병렬이면 풀로) 처리하고 결과를 메인 스레드에서 건별 적재한다.

        한 기업이 _build_lead 에서 예외가 나도 그 기업만 건너뛰고(로그) 나머지·이미 성공한
        기업은 보존한다(배치 전체 유실 방지). 입력 순서로 결과를 받아 적재하므로 leads 순서가
        발견 순서와 같다(workers 무관·순서 결정적; 라이브 내용은 네트워크 의존이라 순차와 동일
        수준으로 비결정). 적재·progress·_emit·DB 세션은 메인 스레드 전담 — 워커가 공유하는
        가변상태는 cost_ledger(자체 락) 뿐이다.
        """
        if not pending:
            return
        futures = [pool.submit(_process_one, d) for d in pending] if pool is not None else None
        for i, d in enumerate(pending):
            try:
                lead = futures[i].result() if futures is not None else _process_one(d)
            except Exception as exc:  # 기업 1건 실패 → 스킵(배치 보존). graceful 아닌 예외만 도달.
                log.warning("pipeline.process.error", key=d.canonical_key, err=str(exc))
                continue
            progress["enriched"] += 1
            leads.append(lead)
            if lead.company.is_active:
                progress["saved"] += 1  # 실존 확인분(persist 면 회사·연락처 저장됨).
            if session is not None:
                _persist_lead(session, d, lead)
            _emit()  # 기업 1건 처리 완료 — 카운터 갱신 통지(폴링 표시).
        pending.clear()

    session: Session | None = get_sessionmaker(settings)() if persist else None
    cancelled = False
    disco_sources: list = []  # finally 가 항상 참조할 수 있게 try 전 바인딩(빌드 실패 시 no-op).
    try:
        # 발견 소스를 런 시작에 1회만 빌드해 모든 세그먼트에 재사용한다(세그먼트마다 재생성·
        # httpx 누수 제거 + keep-alive 연결 재사용). 발견 루프는 단일 스레드라 공유 안전.
        disco_sources = build_sources(settings, cost_ledger)
        if session is not None:
            seen |= load_seen_keys(session)
            seen_domains |= load_seen_domains(session)
        # 인라인 렉시컬 후보 탐지(opt-in, 갭1) — 도메인 없는 신규 기업을 기존 name: 티어와
        # 이름 유사도로 대조해 dedup_candidate(워크벤치)로 적재. 자동 스킵 안 함(제약②).
        lexical_matcher = (
            InlineLexicalMatcher(session)
            if session is not None and settings.dedup_inline_lexical
            else None
        )
        _emit()  # 초기 상태(세그먼트 총수) 통지 — 시작 즉시 진행바가 보이도록.
        for segment in seg_list:
            if should_cancel is not None and should_cancel():
                cancelled = True
                break
            for dc in discover_segment(
                segment,
                settings,
                cost_ledger=cost_ledger,
                sources=disco_sources,
                seen_domains=seen_domains,  # 글로벌 dedup 시드 주입 — 유료 검색 비과금(제약①·②).
            ):
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break  # 다음 기업 처리 전 협조적 중단(처리분은 보존).
                dom = normalize_domain(dc.domain) if dc.domain else None
                if dc.canonical_key in seen or (dom is not None and dom in seen_domains):
                    log.info("dedup.skip", key=dc.canonical_key)
                    if session is not None:
                        # C5 inline 승격(opt-in): **다른 key·같은 도메인**(교차key 중복)이면 단순
                        # touch 대신 생존자에 duplicate_of 링크 — 원장 골든레코드 그래프를 적재
                        # 시점에 완성한다(기존 동작은 cross-key 중복을 미연결 행으로 남김). auto
                        # 티어(이름高+도메인root 일치)만 링크, 그 외·같은key 는 기존대로 touch
                        # (제약② 보수 — 경계는 배치/워크벤치 위임). off 면 항상 touch(회귀 0).
                        linked = False
                        if settings.dedup_inline and dc.canonical_key not in seen and dom is not None:
                            survivor = find_inline_duplicate(session, dc)
                            if survivor is not None:
                                _persist_inline_dup(session, dc, survivor)
                                linked = True
                        if not linked:
                            # 재발견: 추출은 건너뛰되 last_crawled_at 만 갱신(재크롤 추적).
                            _persist_touch(session, dc)
                    continue
                seen.add(dc.canonical_key)
                if dom is not None:
                    seen_domains.add(dom)
                elif resolver is not None:
                    # 도메인 미보유 기업 → 회사명으로 공식 도메인 해석 시도.
                    resolved = resolver.resolve(dc)
                    rdom = normalize_domain(resolved) if resolved else None
                    if rdom is not None:
                        if rdom in seen_domains:
                            # 해석된 도메인이 이미 본 기업과 동치 → 재추출 스킵(제약 ①).
                            # 원장엔 해석된 도메인을 기록해 다음 런이 재해석(quota 낭비) 안 하게.
                            log.info("dedup.skip.resolved", key=dc.canonical_key, domain=rdom)
                            if session is not None:
                                _persist_touch(session, dc.model_copy(update={"domain": resolved}))
                            continue
                        seen_domains.add(rdom)
                        dc = dc.model_copy(update={"domain": resolved})  # 도메인만 채움

                progress["discovered"] += 1  # 중복제외 신규 발견(처리 대상 확정).
                # 도메인 없는(name: 티어) 신규 기업만 렉시컬 후보 대조(워크벤치 적재, 추출은 진행).
                if lexical_matcher is not None and dc.canonical_key.startswith("name:"):
                    lexical_matcher.consider(session, dc.canonical_key, dc.name, dc.country or "")
                pending.append(dc)  # 배치 적재 — _flush 에서 (병렬이면 풀로) 동시 처리.
                if len(pending) >= batch_size:
                    _flush()
                    if _target_hit():
                        break  # 목표 실존수 도달 — 이 세그먼트 내 추가 발견 중단.
            _flush()  # 세그먼트 경계 — 큐된 분 처리(취소 시에도 이미 발견한 분은 보존).
            if cancelled or _target_hit():
                break  # 취소 또는 목표 도달 → 남은 세그먼트는 돌지 않고 종료.
            progress["segments_done"] += 1
            _emit()
    finally:
        if pool is not None:
            pool.shutdown(wait=True)  # 진행 중 워커 완료 대기 후 인스턴스 정리(쓰기 경쟁 없음).
        # 워커별 + 메인스레드(순차 경로) 인스턴스 모두 정리 — close() 있는 것만(best-effort).
        for obj in (*_created, enricher, existence, email_validator):
            close = getattr(obj, "close", None)
            if callable(close):
                close()
        close_sources(disco_sources)  # 발견 소스 httpx 클라이언트 정리(런당 1회).
        if session is not None:
            session.close()
    return leads


def _persist_touch(session: Session, dc: DiscoveredCompany) -> None:
    """재발견 기업의 last_crawled_at 만 갱신(per-company 트랜잭션)."""
    try:
        save_discovered(session, dc)
        session.commit()
    except IntegrityError:
        session.rollback()


def _persist_inline_dup(session: Session, dc: DiscoveredCompany, survivor_key: str) -> None:
    """inline auto-중복으로 판정된 신규 리드를 원장에 기록하고 기존 생존자에 흡수한다(가역).

    원장엔 항상 남기되(제약① 재추출 방지) ``duplicate_of`` + 머지 audit 를 적어 가역
    추적한다. 회사 본체·연락처는 만들지 않는다(추출 스킵). per-company 트랜잭션.
    """
    from datetime import datetime, timezone

    from ..schema import DiscoveredCompanyRow

    try:
        save_discovered(session, dc)
        row = session.get(DiscoveredCompanyRow, dc.canonical_key)
        linked = (
            row is not None and row.duplicate_of is None and dc.canonical_key != survivor_key
        )
        if linked:
            row.duplicate_of = survivor_key
            row.merged_at = datetime.now(timezone.utc)
            row.merged_by = "auto"
            # merge_reason 은 stable 토큰(report/rollback 파싱용) — schema.py 컨벤션 준수.
            row.merge_reason = "inline:name+domain"
        session.commit()
        # 실제 링크가 써졌을 때만 absorb 로그(이미 링크됨/가드 실패 시 오탐 audit 방지).
        if linked:
            log.info("dedup.inline.absorb", key=dc.canonical_key, survivor=survivor_key)
        else:
            log.info("dedup.inline.touch", key=dc.canonical_key)
    except IntegrityError:
        session.rollback()
        log.info("persist.skip.conflict", key=dc.canonical_key)


def _persist_lead(session: Session, dc: DiscoveredCompany, lead: CompanyLead) -> None:
    """한 기업을 독립 트랜잭션으로 영속화한다.

    원장은 항상 기록(제약 ①), 회사 본체는 실존(active)만 저장(제약 ②). 동시 워커가
    같은 기업을 먼저 적재해 PK/UNIQUE 충돌이 나면 해당 기업만 스킵(배치 전체 보호).
    """
    try:
        save_discovered(session, dc)
        if lead.company.is_active:
            save_lead(session, lead, source=dc.source)
        session.commit()
    except IntegrityError:
        session.rollback()
        log.info("persist.skip.conflict", key=dc.canonical_key)
