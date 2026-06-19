"""파이프라인 본체 — 발견부터 CompanyLead 까지.

제약 ①(중복) : ``seen`` canonical_key 집합으로 이미 본 기업을 스킵.
제약 ②(실존) : ExistenceVerifier 로 죽은 기업을 거른다(검증 큐 대상).
dry_run 에서는 모든 단계가 네트워크 없이 결정적으로 동작한다.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..emailrules import select_best_email
from ..enrich.enricher import Enricher
from ..logging import get_logger
from ..models import (
    Company,
    CompanyLead,
    ContactType,
    EmailValidation,
    Listed,
)
from ..sources.base import DiscoveredCompany, Segment
from ..sources.registry import discover_segment
from ..storage.db import get_sessionmaker
from ..storage.repository import load_seen_keys, save_discovered, save_lead
from ..verify.email_validator import EmailValidator
from ..verify.existence import ExistenceVerifier

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
    existence = ExistenceVerifier(settings)
    email_validator = EmailValidator(settings)
    enricher = Enricher(settings)

    leads: list[CompanyLead] = []
    session: Session | None = get_sessionmaker(settings)() if persist else None
    try:
        if session is not None:
            seen |= load_seen_keys(session)
        for segment in segments:
            for dc in discover_segment(segment, settings):
                if dc.canonical_key in seen:
                    log.info("dedup.skip", key=dc.canonical_key)
                    # 재발견: 추출은 건너뛰되 last_crawled_at 은 갱신(재크롤 추적).
                    if session is not None:
                        _persist_touch(session, dc)
                    continue
                seen.add(dc.canonical_key)

                contacts = enricher.enrich(dc)
                email = select_best_email(contacts)
                phone = next((c for c in contacts if c.type is ContactType.PHONE), None)
                form = next((c for c in contacts if c.type is ContactType.FORM), None)

                ex = existence.verify(dc.domain)
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
                validation = (
                    email_validator.validate(email.value, dc.domain)
                    if email
                    else EmailValidation()
                )
                lead = CompanyLead(
                    company=company, email=email, phone=phone, form=form,
                    email_validation=validation,
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
