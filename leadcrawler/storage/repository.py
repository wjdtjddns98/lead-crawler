"""영속화 계층 — 도메인 모델 ↔ schema Row 매핑.

제공 기능:
- :func:`load_seen_keys` : 이미 발견된 ``canonical_key`` 집합(제약 ① dedup seed).
- :func:`save_discovered` : 발견 단계 결과를 멱등 upsert(이미 본 기업은 식별정보 보존,
  ``last_crawled_at`` 만 매 발견 시각으로 갱신).
- :func:`save_lead` : 검증된 :class:`CompanyLead` 를 회사·연락처·이메일검증까지 영속화.

운영 RDB(PostgreSQL)와 테스트 SQLite 모두에서 동일하게 동작하도록 ORM 만 사용한다.

식별자 전략(중요): DB 행 PK 는 ``canonical_key`` 에서 **결정적으로 파생**한다(랜덤 uuid 미사용).
이로써 같은 기업을 24/7 재크롤해도 ``company.id``·``contact.id`` 가 안정적으로 유지되어
연락처·이메일검증 이력이 매 크롤마다 파괴되지 않고, 동시 삽입은 PK/UNIQUE 충돌로 안전하게
차단된다(동시성 처리는 파이프라인의 per-company 트랜잭션이 담당).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    Company,
    CompanyLead,
    Contact,
    ContactType,
    EmailRole,
    EmailValidation,
    ExtractMethod,
    Listed,
    ValidationStatus,
)
from ..emailrules import accepted_emails
from ..schema import (
    CompanyRow,
    ContactRow,
    DiscoveredCompanyRow,
    EmailValidationRow,
    ReviewQueueRow,
)
from ..sources.base import DiscoveredCompany
from .review import (
    candidate_values_of,
    clear_email_review,
    effective_selected,
    enqueue_email_review,
    review_id_for,
)


def company_id_for(canonical_key: str) -> str:
    """canonical_key 로부터 안정적인 회사 PK 를 만든다(재크롤 간 불변)."""
    return "c_" + hashlib.sha1(canonical_key.encode("utf-8")).hexdigest()[:30]


def contact_id_for(company_id: str, contact_type: str, value: str) -> str:
    """(회사·종류·값)으로부터 안정적인 연락처 PK 를 만든다(동일 연락처면 동일 id)."""
    raw = f"{company_id}|{contact_type}|{value}".encode()
    return "k_" + hashlib.sha1(raw).hexdigest()[:30]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 회사명 컬럼(varchar(512)) 초과 입력은 PG 에서 insert 를 거부하므로 안전 절단.
_NAME_MAX = 512


def _clip(value: str, limit: int = _NAME_MAX) -> str:
    """컬럼 길이 초과 문자열을 절단한다(PG StringDataRightTruncation 방지)."""
    return value if len(value) <= limit else value[:limit]


def load_seen_keys(session: Session) -> set[str]:
    """발견 테이블에 적재된 모든 ``canonical_key`` 를 반환한다(제약 ① 시드)."""
    return set(session.scalars(select(DiscoveredCompanyRow.canonical_key)).all())


def save_discovered(session: Session, dc: DiscoveredCompany) -> DiscoveredCompanyRow:
    """발견 기업을 멱등 upsert 한다.

    제약 ①: 이미 존재하는 ``canonical_key`` 의 식별정보는 덮어쓰지 않고 그대로 둔다.
    단, ``last_crawled_at`` 은 신규/기존 모두 매 발견 시각으로 갱신(재크롤 추적용).
    """
    row = session.get(DiscoveredCompanyRow, dc.canonical_key)
    if row is None:
        row = DiscoveredCompanyRow(
            canonical_key=dc.canonical_key,
            name=_clip(dc.name),
            country=dc.country,
            industry=dc.industry,
            listed=dc.listed,
            registry=dc.registry,
            registry_id=dc.registry_id,
            domain=dc.domain,
            segment=dc.segment,
            source=dc.source,
        )
        session.add(row)
    row.last_crawled_at = _utcnow()
    return row


def _ensure_discovered_from_lead(session: Session, lead: CompanyLead, source: str) -> None:
    """리드의 회사 정보로 발견 행을 보장한다(FK 충족용)."""
    c = lead.company
    row = session.get(DiscoveredCompanyRow, c.canonical_key)
    if row is None:
        row = DiscoveredCompanyRow(
            canonical_key=c.canonical_key,
            name=_clip(c.name),
            country=c.country,
            industry=c.industry,
            listed=c.listed.value,
            registry=c.registry,
            registry_id=c.registry_id,
            domain=c.domain,
            segment=c.segment,
            source=source,
        )
        session.add(row)
        row.last_crawled_at = _utcnow()


def save_lead(session: Session, lead: CompanyLead, *, source: str = "") -> CompanyRow:
    """검증된 리드를 회사·연락처·이메일검증까지 멱등 저장한다.

    재저장 시 변하지 않은 연락처·검증행은 **id 가 보존**되어 이력이 유지되고,
    사라진 연락처만 삭제(자식 email_validation 은 명시 삭제 + DB CASCADE 로 정리)된다.
    """
    _ensure_discovered_from_lead(session, lead, source)
    c = lead.company
    cid = company_id_for(c.canonical_key)

    company = session.get(CompanyRow, cid)
    if company is None:
        company = CompanyRow(id=cid, canonical_key=c.canonical_key)
        session.add(company)
    company.name = _clip(c.name)
    company.country = c.country
    company.industry = c.industry
    company.homepage = c.homepage
    company.is_active = c.is_active
    company.existence_confidence = c.existence_confidence
    company.site_alive = c.site_alive
    session.flush()  # company 행 확정(연락처 FK 용)

    # 채택 연락처와 각자의 결정적 id 산정. 이메일은 **전체 후보**를 저장한다(사람이 검증
    # 웹앱에서 최종 선택). email_candidates 가 비면 단일 email 로 폴백(하위호환).
    email_contacts = lead.email_candidates or ([lead.email] if lead.email else [])
    desired: list[tuple[Contact, str]] = [
        (ct, contact_id_for(cid, ct.type.value, ct.value))
        for ct in (*email_contacts, lead.phone, lead.form)
        if ct is not None
    ]
    desired_ids = {det_id for _, det_id in desired}

    # 더 이상 존재하지 않는 연락처만 제거(자식 검증행 먼저 삭제 → FK 안전).
    existing_ids = set(
        session.scalars(
            select(ContactRow.id).where(ContactRow.company_id == cid)
        ).all()
    )
    stale_ids = list(existing_ids - desired_ids)
    if stale_ids:
        session.execute(
            delete(EmailValidationRow).where(EmailValidationRow.contact_id.in_(stale_ids))
        )
        session.execute(delete(ContactRow).where(ContactRow.id.in_(stale_ids)))
        session.flush()

    # 남는/새 연락처는 결정적 id 로 upsert(이력 보존).
    for contact, det_id in desired:
        row = session.get(ContactRow, det_id)
        if row is None:
            row = ContactRow(id=det_id, company_id=cid)
            session.add(row)
        row.type = contact.type.value
        row.value = contact.value
        row.role = contact.role.value
        row.extract_method = contact.extract_method.value
        row.confidence = contact.confidence
    session.flush()

    # 이메일 검증은 각 후보 연락처에 1:1(contact_id PK) 로 in-place upsert.
    for ct in email_contacts:
        email_id = contact_id_for(cid, ct.type.value, ct.value)
        v = lead.email_validations.get(ct.value) or (
            lead.email_validation if lead.email and ct.value == lead.email.value else None
        )
        if v is None:
            continue
        ev = session.get(EmailValidationRow, email_id)
        if ev is None:
            ev = EmailValidationRow(contact_id=email_id)
            session.add(ev)
        ev.status = v.status.value
        ev.mx = v.mx
        ev.domain_match = v.domain_match
        ev.smtp = v.smtp
        ev.provider = v.provider
        ev.checked_at = v.checked_at

    # enqueue 규칙: 이메일 후보가 있으면 전체 후보를 검증 큐에 등록(상태·선택 보존 멱등).
    # 재크롤로 후보가 0건이 되면 기존 큐 행의 후보를 비운다(죽은 후보 잔존 방지).
    if email_contacts:
        default = lead.email.value if lead.email is not None else email_contacts[0].value
        enqueue_email_review(
            session, cid, [ct.value for ct in email_contacts], selected_default=default
        )
    else:
        clear_email_review(session, cid)
    return company


def _contact_of(row: ContactRow) -> Contact:
    """ContactRow 를 도메인 :class:`Contact` 로 복원한다(enum 안전 변환)."""
    return Contact(
        id=row.id,
        type=ContactType(row.type),
        value=row.value,
        role=EmailRole(row.role),
        extract_method=ExtractMethod(row.extract_method),
        confidence=row.confidence,
    )


def load_leads(
    session: Session, *, company_ids: list[str] | None = None
) -> list[CompanyLead]:
    """DB 행을 :class:`CompanyLead` 로 복원한다(엑셀 export 전용).

    ``company_ids`` 가 주어지면 해당 회사만(확정분 export 등), 없으면 전체를 적재한다.
    회사명 정렬로 결정적 순서를 보장한다. **재저장(save_lead)용이 아니다** — 선택된
    이메일 1건만 채우고 ``email_candidates``/``email_validations`` 는 복원하지 않으므로,
    이 결과를 다시 save_lead 에 넘기면 나머지 후보가 stale 로 삭제된다.
    """
    stmt = select(CompanyRow).order_by(CompanyRow.name)
    if company_ids is not None:
        if not company_ids:
            return []
        stmt = stmt.where(CompanyRow.id.in_(company_ids))

    leads: list[CompanyLead] = []
    for crow in session.scalars(stmt).all():
        disc = session.get(DiscoveredCompanyRow, crow.canonical_key)
        company = Company(
            id=crow.id,
            canonical_key=crow.canonical_key,
            name=crow.name,
            country=crow.country,
            industry=crow.industry,
            listed=Listed(disc.listed) if disc else Listed.UNKNOWN,
            homepage=crow.homepage,
            domain=disc.domain if disc else None,
            is_active=crow.is_active,
            existence_confidence=crow.existence_confidence,
            site_alive=crow.site_alive,
        )
        contacts = session.scalars(
            select(ContactRow).where(ContactRow.company_id == crow.id)
        ).all()
        emails = [_contact_of(r) for r in contacts if r.type == ContactType.EMAIL.value]
        phone = next(
            (_contact_of(r) for r in contacts if r.type == ContactType.PHONE.value), None
        )
        form = next(
            (_contact_of(r) for r in contacts if r.type == ContactType.FORM.value), None
        )
        # export 는 사람이 검증 웹앱에서 고른 이메일을 쓴다. DTO 표시와 **동일 규칙**
        # (effective_selected) 으로 골라 워크벤치 표시 ≠ export 불일치를 막는다.
        rq = session.get(ReviewQueueRow, review_id_for(crow.id, "email"))
        chosen = (
            effective_selected(rq.selected, candidate_values_of(rq)) if rq is not None else None
        )
        email = next((e for e in emails if e.value == chosen), None)
        if email is None:  # 큐 행 없음·후보 미영속 등 → best 후보로 최후 폴백.
            ranked = accepted_emails(emails)
            email = ranked[0] if ranked else (emails[0] if emails else None)

        validation = EmailValidation()
        if email is not None:
            evrow = session.get(EmailValidationRow, email.id)
            if evrow is not None:
                validation = EmailValidation(
                    status=ValidationStatus(evrow.status),
                    mx=evrow.mx,
                    domain_match=evrow.domain_match,
                    smtp=evrow.smtp,
                    provider=evrow.provider,
                    checked_at=evrow.checked_at,
                )
        leads.append(
            CompanyLead(
                company=company,
                email=email,
                phone=phone,
                form=form,
                email_validation=validation,
            )
        )
    return leads
