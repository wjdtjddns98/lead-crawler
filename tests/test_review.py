"""검증 큐 스토리지 계층 테스트 — enqueue 멱등·상태 보존·조회."""

from __future__ import annotations

import pytest
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
from leadcrawler.schema import ContactRow, EmailValidationRow
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import save_lead
from leadcrawler.storage.review import (
    CONFIRMED,
    count_reviews,
    enqueue_email_review,
    get_review,
    query_reviews,
    review_id_for,
    set_review_status,
)


@pytest.fixture
def session(tmp_path) -> Session:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/rev.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        yield s


def _lead(domain: str = "acme.com", email: str | None = "ir@acme.com") -> CompanyLead:
    company = Company(
        canonical_key=f"dom:{domain}",
        name="아크메",
        country="KR",
        industry="건설",
        domain=domain,
        homepage=f"https://{domain}",
        is_active=True,
        site_alive=True,
    )
    ct = (
        Contact(type=ContactType.EMAIL, value=email, role=EmailRole.IR)
        if email
        else None
    )
    return CompanyLead(
        company=company,
        email=ct,
        email_validation=EmailValidation(status=ValidationStatus.VALID, mx=True, smtp=True),
    )


def test_save_lead_auto_enqueues(session: Session) -> None:
    save_lead(session, _lead())
    session.flush()
    assert count_reviews(session) == 1
    items = query_reviews(session)
    assert items[0]["candidates"] == ["ir@acme.com"]
    assert items[0]["status"] == "pending"
    assert items[0]["name"] == "아크메"
    assert items[0]["email_status"] == "valid"
    assert items[0]["email_smtp"] is True


def test_no_email_no_enqueue(session: Session) -> None:
    save_lead(session, _lead(email=None))
    session.flush()
    assert count_reviews(session) == 0


def test_enqueue_preserves_status_on_recrawl(session: Session) -> None:
    save_lead(session, _lead())
    session.flush()
    rid = query_reviews(session)[0]["id"]
    set_review_status(session, rid, CONFIRMED, assignee="정성운")
    session.flush()
    # 재크롤(재저장) — 확정 상태가 pending 으로 되돌아가면 안 됨.
    save_lead(session, _lead())
    session.flush()
    item = get_review(session, rid)
    assert item["status"] == CONFIRMED
    assert item["assignee"] == "정성운"


def test_enqueue_updates_candidates_only(session: Session) -> None:
    save_lead(session, _lead())  # 실제 회사 생성(review_queue FK 충족)
    session.flush()
    item = query_reviews(session)[0]
    cid, rid = item["company_id"], item["id"]
    assert rid == review_id_for(cid, "email")
    set_review_status(session, rid, CONFIRMED)
    enqueue_email_review(session, cid, ["ir@acme.com", "contact@acme.com"])
    session.flush()
    item2 = get_review(session, rid)
    assert item2["candidates"] == ["ir@acme.com", "contact@acme.com"]
    assert item2["status"] == CONFIRMED  # 상태 보존


def test_status_filter_and_count(session: Session) -> None:
    save_lead(session, _lead("a.com", "ir@a.com"))
    save_lead(session, _lead("b.com", "ir@b.com"))
    session.flush()
    rid = query_reviews(session, status="pending")[0]["id"]
    set_review_status(session, rid, CONFIRMED)
    session.flush()
    assert count_reviews(session, status="pending") == 1
    assert count_reviews(session, status="confirmed") == 1
    assert len(query_reviews(session, status="confirmed")) == 1


def test_set_status_invalid_raises(session: Session) -> None:
    save_lead(session, _lead())
    session.flush()
    rid = query_reviews(session)[0]["id"]
    with pytest.raises(ValueError, match="허용되지 않은 상태"):
        set_review_status(session, rid, "bogus")


def test_set_status_missing_returns_none(session: Session) -> None:
    assert set_review_status(session, "r_nonexistent", CONFIRMED) is None


def test_multi_email_no_fanout(session: Session) -> None:
    """회사가 이메일 연락처를 둘 가져도 큐 행은 1개로 유지(조인 행폭증 방지)."""
    save_lead(session, _lead())  # 회사 + 이메일 ir@acme.com(valid) + 큐행
    session.flush()
    cid = query_reviews(session)[0]["company_id"]
    # 파이프라인 외 경로로 두 번째 이메일+검증을 직접 추가(대표는 id 최소 = 원본).
    session.add(
        ContactRow(id="k_zzzz_second", company_id=cid, type="email", value="info@acme.com")
    )
    session.add(EmailValidationRow(contact_id="k_zzzz_second", status="invalid"))
    session.flush()
    items = query_reviews(session)
    assert len(items) == 1  # 1:1 유지 — 중복 행 없음
    assert items[0]["email_status"] == "valid"  # 결정적 대표 신호
    assert get_review(session, items[0]["id"])["email_status"] == "valid"  # 목록=단건 일치
