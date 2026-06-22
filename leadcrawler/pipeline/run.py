"""파이프라인 본체 — 발견부터 CompanyLead 까지.

제약 ①(중복) : ``seen``(canonical_key) + ``seen_domains``(정규화 도메인) 집합으로 이미
본 기업을 스킵 — 같은 기업이 reg:/dom: 등 다른 key 로 잡혀도 도메인 동치로 한 번만 추출.
제약 ②(실존) : ExistenceVerifier 로 죽은 기업을 거른다(검증 큐 대상).
dry_run 에서는 모든 단계가 네트워크 없이 결정적으로 동작한다.
"""

from __future__ import annotations

from collections.abc import Iterable

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
from ..sources.base import DiscoveredCompany, Segment
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
) -> list[CompanyLead]:
    """세그먼트들을 처리해 검증된 :class:`CompanyLead` 목록을 반환한다.

    ``persist=True`` 면 DB 세션을 열어 ① 발견 원장(discovered_company)에 모든 신규
    기업을 기록(죽은 기업도 — 제약 ① 재추출 방지)하고, ② 실존(active) 기업만 회사·
    연락처 테이블에 저장한다(제약 ②). 기존 ``seen`` 은 원장 key 와 합쳐 dedup 시드가 된다.
    """
    settings = settings or get_settings()
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

    leads: list[CompanyLead] = []
    session: Session | None = get_sessionmaker(settings)() if persist else None
    try:
        if session is not None:
            seen |= load_seen_keys(session)
            seen_domains |= load_seen_domains(session)
        for segment in segments:
            for dc in discover_segment(segment, settings):
                dom = normalize_domain(dc.domain) if dc.domain else None
                if dc.canonical_key in seen or (dom is not None and dom in seen_domains):
                    log.info("dedup.skip", key=dc.canonical_key)
                    # 재발견: 추출은 건너뛰되 last_crawled_at 은 갱신(재크롤 추적).
                    if session is not None:
                        _persist_touch(session, dc)
                    continue
                seen.add(dc.canonical_key)
                if dom is not None:
                    seen_domains.add(dom)

                contacts = enricher.enrich(dc)
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

                if session is not None:
                    _persist_lead(session, dc, lead)
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
