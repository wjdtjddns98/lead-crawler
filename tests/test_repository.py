"""영속화 계층(repository) 테스트 — 매핑·멱등 upsert·dedup seed."""

from __future__ import annotations

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from leadcrawler.config import Settings
from leadcrawler.models import (
    Company,
    CompanyLead,
    Contact,
    ContactType,
    EmailRole,
    EmailValidation,
    ValidationStatus,
)
from leadcrawler.schema import CompanyRow, ContactRow, DiscoveredCompanyRow, EmailValidationRow
from leadcrawler.sources.base import DiscoveredCompany
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import (
    load_leads,
    load_seen_domains,
    load_seen_keys,
    save_discovered,
    save_lead,
)


@pytest.fixture
def session(tmp_path) -> Session:
    """격리된 파일 SQLite 세션(db.py 경로 — FK 강제 ON, 테스트마다 새 스키마)."""
    settings = Settings(database_url=f"sqlite:///{tmp_path}/repo.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        yield s


def _lead(domain: str = "x.com", *, with_contacts: bool = True) -> CompanyLead:
    company = Company(
        canonical_key=f"dom:{domain}",
        name="테스트기업",
        country="KR",
        industry="건설",
        domain=domain,
        homepage=f"https://{domain}",
        is_active=True,
    )
    if not with_contacts:
        return CompanyLead(company=company)
    email = Contact(type=ContactType.EMAIL, value=f"ir@{domain}", role=EmailRole.IR)
    phone = Contact(type=ContactType.PHONE, value="+82-2-0000-0000")
    form = Contact(type=ContactType.FORM, value=f"https://{domain}/contact")
    validation = EmailValidation(status=ValidationStatus.VALID, mx=True, domain_match=True)
    return CompanyLead(
        company=company, email=email, phone=phone, form=form, email_validation=validation
    )


def test_load_leads_is_batched_not_n_plus_1(session: Session) -> None:
    """load_leads 가 회사당 N+1 이 아니라 IN 배치 — 회사 수가 늘어도 쿼리수 일정(O(1))."""
    for i in range(12):
        save_lead(session, _lead(f"c{i}.com"))
    session.flush()

    count = 0
    bind = session.get_bind()

    def _before(*_a, **_k) -> None:
        nonlocal count
        count += 1

    event.listen(bind, "before_cursor_execute", _before)
    try:
        leads = load_leads(session)
    finally:
        event.remove(bind, "before_cursor_execute", _before)

    # 산출 정확성: 12개 회사 전부 복원 + 이메일/폼/검증 채워짐.
    assert len(leads) == 12
    assert all(ld.email is not None and ld.email.value.startswith("ir@") for ld in leads)
    assert all(ld.form is not None for ld in leads)
    assert any(ld.email_validation.status is ValidationStatus.VALID for ld in leads)
    # 쿼리수는 배치라 상수(회사 + disc + contacts + review_queue + email_validation ≈ 5).
    # 회사당 ~4N(=48) 였다면 절대 통과 못 한다. 여유 8 로 단언.
    assert count <= 8, f"load_leads 가 {count} 쿼리 — N+1 회귀 의심(배치 깨짐)"


def test_save_discovered_is_idempotent(session: Session) -> None:
    dc = DiscoveredCompany(
        canonical_key="dom:x.com", name="A", country="KR", domain="x.com", source="dart"
    )
    save_discovered(session, dc)
    save_discovered(session, dc)  # 두 번 호출해도 1행(제약 ①).
    session.commit()
    assert load_seen_keys(session) == {"dom:x.com"}


def test_load_seen_domains_returns_normalized(session: Session) -> None:
    # 도메인 동치 dedup 시드 — 정규화 도메인만, 도메인 없는 행은 제외(제약 ①).
    save_discovered(session, DiscoveredCompany(
        canonical_key="reg:dart:001", name="삼성", country="KR",
        domain="https://www.samsung.com", registry="dart", registry_id="001", source="dart",
    ))
    save_discovered(session, DiscoveredCompany(
        canonical_key="reg:lei:xyz", name="도메인없음", country="KR",
        domain=None, registry="lei", registry_id="xyz", source="gleif",
    ))
    session.commit()
    assert load_seen_domains(session) == {"samsung.com"}


def test_save_discovered_sets_listed_and_last_crawled_at(session: Session) -> None:
    dc = DiscoveredCompany(
        canonical_key="dom:x.com", name="A", domain="x.com", source="dart", listed="listed"
    )
    save_discovered(session, dc)
    session.commit()
    row = session.get(DiscoveredCompanyRow, "dom:x.com")
    assert row is not None
    assert row.listed == "listed"
    assert row.last_crawled_at is not None


def test_dead_company_is_ledger_only(session: Session) -> None:
    # 죽은 기업: 발견 원장(제약 ①)엔 들어가고 company(제약 ②)엔 들어가지 않는다.
    dc = DiscoveredCompany(
        canonical_key="dom:dead.com", name="죽은기업", domain="dead.com", source="dart"
    )
    save_discovered(session, dc)
    session.commit()
    assert load_seen_keys(session) == {"dom:dead.com"}
    assert session.scalars(select(CompanyRow)).all() == []


def test_save_lead_persists_company_contacts_validation(session: Session) -> None:
    save_lead(session, _lead("x.com"), source="dart")
    session.commit()

    companies = session.scalars(select(CompanyRow)).all()
    assert len(companies) == 1
    contacts = session.scalars(select(ContactRow)).all()
    assert {c.type for c in contacts} == {"email", "phone", "form"}
    ev = session.scalars(select(EmailValidationRow)).all()
    assert len(ev) == 1 and ev[0].status == "valid"
    # 발견 원장도 함께 보장됐는지(FK).
    assert load_seen_keys(session) == {"dom:x.com"}


def test_save_lead_is_idempotent_on_resave(session: Session) -> None:
    save_lead(session, _lead("x.com"), source="dart")
    session.commit()
    ids_before = sorted(session.scalars(select(ContactRow.id)).all())
    ev_before = sorted(session.scalars(select(EmailValidationRow.contact_id)).all())

    # 같은 회사 재저장(새 도메인 객체·새 uuid) — 행이 중복 누적되지 않는다.
    save_lead(session, _lead("x.com"), source="dart")
    session.commit()
    assert len(session.scalars(select(CompanyRow)).all()) == 1
    assert len(session.scalars(select(ContactRow)).all()) == 3
    assert len(session.scalars(select(EmailValidationRow)).all()) == 1

    # 결정적 id 라 contact·email_validation 식별자가 보존된다(이력 유지, C1 회귀가드).
    assert sorted(session.scalars(select(ContactRow.id)).all()) == ids_before
    assert sorted(session.scalars(select(EmailValidationRow.contact_id)).all()) == ev_before


def test_save_discovered_updates_last_crawled_on_existing(session: Session) -> None:
    dc = DiscoveredCompany(
        canonical_key="dom:x.com", name="A", domain="x.com", source="dart"
    )
    save_discovered(session, dc)
    session.commit()
    first = session.get(DiscoveredCompanyRow, "dom:x.com").last_crawled_at
    # 기존 행 재발견 — last_crawled_at 이 전진해야 한다(M3 회귀가드).
    save_discovered(session, dc)
    session.commit()
    second = session.get(DiscoveredCompanyRow, "dom:x.com").last_crawled_at
    assert second >= first


def test_no_duplicate_company_for_same_key(session: Session) -> None:
    # canonical_key 가 같으면 결정적 PK 라 회사 행은 항상 1개(M1 회귀가드).
    save_lead(session, _lead("x.com"), source="dart")
    save_lead(session, _lead("x.com"), source="edgar")
    session.commit()
    assert len(session.scalars(select(CompanyRow)).all()) == 1


def test_save_lead_without_email_has_no_validation(session: Session) -> None:
    save_lead(session, _lead("y.com", with_contacts=False), source="dart")
    session.commit()
    assert len(session.scalars(select(CompanyRow)).all()) == 1
    assert session.scalars(select(EmailValidationRow)).all() == []
