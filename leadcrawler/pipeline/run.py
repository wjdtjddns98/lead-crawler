"""파이프라인 본체 — 발견부터 CompanyLead 까지.

제약 ①(중복) : ``seen``(canonical_key) + ``seen_domains``(정규화 도메인) 집합으로 이미
본 기업을 스킵 — 같은 기업이 reg:/dom: 등 다른 key 로 잡혀도 도메인 동치로 한 번만 추출.
제약 ②(실존) : ExistenceVerifier 로 죽은 기업을 거른다(검증 큐 대상).
dry_run 에서는 모든 단계가 네트워크 없이 결정적으로 동작한다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

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
from ..sources.base import DiscoveredCompany, Segment
from ..sources.domain_resolver import DomainResolver
from ..sources.registry import discover_segment
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


def run_pipeline(
    segments: Iterable[Segment],
    *,
    seen: set[str] | None = None,
    settings: Settings | None = None,
    persist: bool = False,
    on_progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[CompanyLead]:
    """세그먼트들을 처리해 검증된 :class:`CompanyLead` 목록을 반환한다.

    ``persist=True`` 면 DB 세션을 열어 ① 발견 원장(discovered_company)에 모든 신규
    기업을 기록(죽은 기업도 — 제약 ① 재추출 방지)하고, ② 실존(active) 기업만 회사·
    연락처 테이블에 저장한다(제약 ②). 기존 ``seen`` 은 원장 key 와 합쳐 dedup 시드가 된다.

    ``on_progress`` 가 주어지면 발견/보강/저장·세그먼트 진행 카운터 dict 를 단계마다
    호출한다(웹 직접 크롤의 실시간 현황). ``should_cancel`` 이 매 기업 처리 직전 True 를
    반환하면 협조적으로 중단한다(이미 처리된 결과는 보존). 둘 다 None 이면 기존 동작 그대로.
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

    leads: list[CompanyLead] = []
    session: Session | None = get_sessionmaker(settings)() if persist else None
    cancelled = False
    try:
        if session is not None:
            seen |= load_seen_keys(session)
            seen_domains |= load_seen_domains(session)
        _emit()  # 초기 상태(세그먼트 총수) 통지 — 시작 즉시 진행바가 보이도록.
        for segment in seg_list:
            if should_cancel is not None and should_cancel():
                cancelled = True
                break
            for dc in discover_segment(segment, settings, cost_ledger=cost_ledger):
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
                contacts = enricher.enrich(dc)
                progress["enriched"] += 1
                candidates = accepted_emails(contacts)
                email = candidates[0] if candidates else None
                phone = next((c for c in contacts if c.type is ContactType.PHONE), None)
                form = next((c for c in contacts if c.type is ContactType.FORM), None)

                ex = existence.verify(
                    dc.domain, registry=dc.registry, registry_id=dc.registry_id
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
                # 후보별 검증(MX/도메인/SMTP·딜리버러빌리티 opt-in) — 선택 UI 에 신호 제공.
                validations = {
                    c.value: email_validator.validate(c.value, dc.domain) for c in candidates
                }
                validation = (
                    validations.get(email.value, EmailValidation()) if email else EmailValidation()
                )
                lead = CompanyLead(
                    company=company, email=email, email_candidates=candidates,
                    phone=phone, form=form,
                    email_validation=validation, email_validations=validations,
                )
                leads.append(lead)
                if lead.company.is_active:
                    progress["saved"] += 1  # 실존 확인분(persist 면 회사·연락처 저장됨).

                if session is not None:
                    _persist_lead(session, dc, lead)
                _emit()  # 기업 1건 처리 완료 — 카운터 갱신 통지(폴링 표시).
            if cancelled:
                break
            progress["segments_done"] += 1
            _emit()
    finally:
        enricher.close()
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
        if row is not None and row.duplicate_of is None and dc.canonical_key != survivor_key:
            row.duplicate_of = survivor_key
            row.merged_at = datetime.now(timezone.utc)
            row.merged_by = "auto"
            row.merge_reason = "inline near-dup (이름高+도메인root 일치)"
        session.commit()
        log.info("dedup.inline.absorb", key=dc.canonical_key, survivor=survivor_key)
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
