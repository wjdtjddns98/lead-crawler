"""목 시드 데이터 테스트 — 건수·제약 준수·멱등."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from leadcrawler.config import Settings
from leadcrawler.models import ACCEPTED_EMAIL_ROLES, ContactType, EmailRole
from leadcrawler.schema import CompanyRow, ContactRow
from leadcrawler.seed import build_mock_leads, seed_mock_leads
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.review import PENDING, count_reviews


@pytest.fixture
def session(tmp_path) -> Session:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/seed.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        yield s


def test_builds_five_active_leads() -> None:
    """5건이며 전부 실존(active+도메인 생존) — 제약 ②."""
    leads = build_mock_leads()
    assert len(leads) == 5
    for lead in leads:
        assert lead.company.is_active is True
        assert lead.company.site_alive is True
        assert lead.company.canonical_key  # 식별키 존재


def test_email_roles_exclude_hr_and_press() -> None:
    """이메일 후보는 IR/general 만 — HR·언론 배제."""
    for lead in build_mock_leads():
        for cand in lead.email_candidates:
            assert cand.role in ACCEPTED_EMAIL_ROLES
            assert cand.role not in {EmailRole.HR, EmailRole.PRESS}


def test_form_only_lead_present() -> None:
    """이메일 없이 문의폼만 있는 리드가 1건 있다(엑셀 J='사이트 내 문의폼' 경로)."""
    form_only = [
        lead for lead in build_mock_leads() if not lead.email_candidates and lead.form is not None
    ]
    assert len(form_only) == 1
    assert form_only[0].form.type is ContactType.FORM


def test_seed_persists_and_enqueues(session: Session) -> None:
    """적재 시 회사·연락처가 저장되고 실존 리드 전부가 검증 큐에 오른다."""
    count = seed_mock_leads(session)
    session.flush()
    assert count == 5
    assert session.scalar(select(CompanyRow).where(CompanyRow.is_active.is_(True)).limit(1))
    # 회사 5곳 모두 큐 등록(이메일 없어도 — 폼 회사 포함).
    assert count_reviews(session, status=PENDING) == 5
    # 문의폼 회사는 form 연락처를 보유.
    forms = session.scalars(
        select(ContactRow).where(ContactRow.type == ContactType.FORM.value)
    ).all()
    assert len(forms) == 1


def test_seed_is_idempotent(session: Session) -> None:
    """두 번 적재해도 회사·큐 행이 늘지 않는다(canonical_key 멱등)."""
    seed_mock_leads(session)
    session.flush()
    companies_first = session.scalar(select(CompanyRow.id).limit(1))
    first = len(session.scalars(select(CompanyRow.id)).all())
    seed_mock_leads(session)
    session.flush()
    second = len(session.scalars(select(CompanyRow.id)).all())
    assert companies_first is not None
    assert first == second == 5
    assert count_reviews(session) == 5
