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
from leadcrawler.storage.repository import load_leads, save_lead
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
    assert [c["value"] for c in items[0]["candidates"]] == ["ir@acme.com"]
    assert items[0]["selected"] == "ir@acme.com"  # 기본 선택 = best 후보
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
    assert [c["value"] for c in item2["candidates"]] == ["ir@acme.com", "contact@acme.com"]
    assert item2["selected"] == "ir@acme.com"  # 기존 선택이 후보에 남아 보존
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


def test_enqueue_default_selected_and_candidate_signals(session: Session) -> None:
    # 두 후보를 직접 enqueue + 각자 검증행 → DTO 후보별 신호·선택 확인.
    save_lead(session, _lead())  # 회사 생성
    session.flush()
    cid = query_reviews(session)[0]["company_id"]
    # 두번째 후보 contact + 검증(invalid) 추가.
    from leadcrawler.storage.repository import contact_id_for

    cid2 = contact_id_for(cid, "email", "info@acme.com")
    session.add(ContactRow(id=cid2, company_id=cid, type="email", value="info@acme.com"))
    session.add(EmailValidationRow(contact_id=cid2, status="invalid", mx=False))
    enqueue_email_review(
        session, cid, ["ir@acme.com", "info@acme.com"], selected_default="ir@acme.com"
    )
    session.flush()
    item = get_review(session, review_id_for(cid, "email"))
    assert item["selected"] == "ir@acme.com"
    by_val = {c["value"]: c for c in item["candidates"]}
    assert by_val["ir@acme.com"]["email_status"] == "valid"
    assert by_val["info@acme.com"]["email_status"] == "invalid"
    assert item["email_status"] == "valid"  # 대표 = 선택(ir)의 신호


def test_confirm_with_selection(session: Session) -> None:
    save_lead(session, _lead())
    session.flush()
    cid = query_reviews(session)[0]["company_id"]
    rid = review_id_for(cid, "email")
    session.add(
        ContactRow(id="k_info_2", company_id=cid, type="email", value="info@acme.com")
    )
    enqueue_email_review(session, cid, ["ir@acme.com", "info@acme.com"])
    session.flush()
    item = set_review_status(session, rid, CONFIRMED, assignee="심사원", selected="info@acme.com")
    assert item["selected"] == "info@acme.com" and item["status"] == CONFIRMED


def test_select_out_of_candidates_raises(session: Session) -> None:
    save_lead(session, _lead())
    session.flush()
    rid = query_reviews(session)[0]["id"]
    with pytest.raises(ValueError, match="후보에 없는 선택"):
        set_review_status(session, rid, CONFIRMED, selected="nope@x.com")


def test_selection_reset_when_candidate_vanishes(session: Session) -> None:
    # 사람이 고른 선택이 재크롤에서 후보 목록에서 사라지면 기본값으로 재설정.
    save_lead(session, _lead())
    session.flush()
    cid = query_reviews(session)[0]["company_id"]
    rid = review_id_for(cid, "email")
    session.add(
        ContactRow(id="k_old_sel", company_id=cid, type="email", value="old@acme.com")
    )
    enqueue_email_review(session, cid, ["ir@acme.com", "old@acme.com"])
    set_review_status(session, rid, CONFIRMED, selected="old@acme.com")
    session.flush()
    # 재크롤: old@ 후보가 사라짐 → 선택이 기본(ir)로 재설정.
    enqueue_email_review(session, cid, ["ir@acme.com"], selected_default="ir@acme.com")
    session.flush()
    assert get_review(session, rid)["selected"] == "ir@acme.com"


def _multi_lead() -> CompanyLead:
    """이메일 후보 2개(ir best + info)를 가진 리드."""
    company = Company(
        canonical_key="dom:acme.com", name="아크메", country="KR", industry="건설",
        domain="acme.com", homepage="https://acme.com", is_active=True, site_alive=True,
    )
    ir = Contact(type=ContactType.EMAIL, value="ir@acme.com", role=EmailRole.IR, confidence=0.9)
    info = Contact(
        type=ContactType.EMAIL, value="info@acme.com", role=EmailRole.GENERAL, confidence=0.5
    )
    return CompanyLead(
        company=company, email=ir, email_candidates=[ir, info],
        email_validation=EmailValidation(status=ValidationStatus.VALID, mx=True),
        email_validations={
            "ir@acme.com": EmailValidation(status=ValidationStatus.VALID, mx=True),
            "info@acme.com": EmailValidation(status=ValidationStatus.RISKY, mx=True),
        },
    )


def test_multi_candidate_save_select_export(session: Session) -> None:
    save_lead(session, _multi_lead())
    session.flush()
    item = query_reviews(session)[0]
    rid, cid = item["id"], item["company_id"]
    # 두 후보 모두 저장 + 기본 선택 = best(ir).
    assert {c["value"] for c in item["candidates"]} == {"ir@acme.com", "info@acme.com"}
    assert item["selected"] == "ir@acme.com"
    # export(load_leads) 는 기본 선택(ir)을 쓴다.
    assert load_leads(session, company_ids=[cid])[0].email.value == "ir@acme.com"
    # 사람이 info 로 변경 확정 → export 가 info 를 반영.
    set_review_status(session, rid, CONFIRMED, selected="info@acme.com")
    session.flush()
    assert load_leads(session, company_ids=[cid])[0].email.value == "info@acme.com"


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
