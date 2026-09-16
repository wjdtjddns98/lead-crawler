"""도메인 오배정 판정·계획·적용·되돌리기(SQLite) — docs/domain-conflict-design-2026-09-16.md §6."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from leadcrawler.config import Settings
from leadcrawler.dedup_resolve.domain_conflict import (
    ALLOW_SHARED,
    AUTO_WRONG,
    OWNER,
    RELATED,
    REVIEW,
    SAME_ENTITY,
    ConflictPlan,
    apply_row,
    build_plan,
    classify_group,
    load_conflict_groups,
    rollback_row,
)
from leadcrawler.schema import (
    CompanyRow,
    ContactRow,
    DiscoveredCompanyRow,
    EmailValidationRow,
    ReviewAuditRow,
    ReviewQueueRow,
)
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import contact_id_for
from leadcrawler.storage.review import enqueue_email_review, review_id_for


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/dc.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        yield s


def _row(cid, key, name, *, host="yesform.com", reg_no=None, name_eng=None, country="KR"):
    return {
        "company_id": cid, "canonical_key": key, "name": name, "country": country, "host": host,
        "homepage": f"https://{host}", "site_alive": True, "ledger_domain": host,
        "reg_no": reg_no, "name_eng": name_eng,
    }


# ── 판정(순수) ──────────────────────────────────────────────────────────────────
def test_dom_key_owner_and_korean_outsider_is_auto_wrong() -> None:
    rows = [
        _row("c1", "dom:yesform.com", "주식회사 예스폼"),
        _row("c2", "name:kr:향우종합관리", "향우종합관리(주)"),
    ]
    out = {c.company_id: c for c in classify_group(rows, {})}
    assert out["c1"].action == OWNER and out["c1"].evidence == ["dom_key"]
    assert out["c2"].action == AUTO_WRONG and out["c2"].evidence == []


def test_same_entity_outsider_left_to_dedup() -> None:
    rows = [_row("c1", "dom:yesform.com", "주식회사 예스폼"), _row("c2", "name:kr:예스폼", "(주)예스폼")]
    out = {c.company_id: c for c in classify_group(rows, {})}
    assert out["c2"].action == SAME_ENTITY


def test_no_owner_means_review() -> None:
    rows = [
        _row("c1", "reg:dart:1", "삼지토건(주)", host="samji.com", reg_no="1"),
        _row("c2", "name:kr:삼지전자", "삼지전자(주)", host="samji.com"),
    ]
    assert {c.action for c in classify_group(rows, {})} == {REVIEW}


def test_two_owners_allow_shared_and_outsider_auto_wrong() -> None:
    rows = [
        _row("c1", "dom:daishin.com", "대신증권(주)", host="daishin.com", reg_no="1"),
        _row("c2", "reg:dart:2", "대신에이엠씨(주)", host="daishin.com", reg_no="2"),
        _row("c3", "name:kr:한빛유통", "한빛유통", host="daishin.com"),
    ]
    audited = {"c2": {"daishin.com"}}  # 사람이 워크벤치에서 확정한 홈페이지
    out = {c.company_id: c for c in classify_group(rows, audited)}
    assert out["c1"].action == ALLOW_SHARED and out["c2"].action == ALLOW_SHARED
    assert out["c2"].evidence == ["audited_homepage"]
    assert out["c3"].action == AUTO_WRONG


def test_latin_name_and_title_evidence() -> None:
    rows = [
        _row("c1", "reg:sec:1", "Celsius Holdings, Inc.", host="celsiusholdingsinc.com", country="US"),
        _row("c2", "name:kr:테라젠이텍스", "(주)테라젠이텍스", host="etexpharm.co.kr"),
        _row("c3", "name:kr:다른회사", "다른회사(주)", host="etexpharm.co.kr"),
        _row("c4", "name:kr:코셈", "(주)코셈", host="coxem.com"),  # 2글자 상호는 title 근거 불인정
    ]
    titles = {"etexpharm.co.kr": "테라젠이텍스 | 제약 전문기업", "coxem.com": "코셈 | 전자현미경"}
    out = {}
    for host in ("celsiusholdingsinc.com", "etexpharm.co.kr", "coxem.com"):  # 그룹 = host 단위
        out.update({c.company_id: c for c in classify_group([r for r in rows if r["host"] == host], {}, titles)})
    assert "latin_name" in out["c1"].evidence
    assert out["c2"].action == OWNER and out["c2"].evidence == ["title"]
    assert out["c3"].action == AUTO_WRONG
    assert out["c4"].evidence == []


def test_related_prefix_and_registry_rows_are_not_auto_wrong() -> None:
    rows = [
        _row("c1", "dom:alteogen.com", "(주)알테오젠", host="alteogen.com"),
        _row("c2", "name:kr:알테오젠바이오로직스", "주식회사 알테오젠바이오로직스", host="alteogen.com"),
        _row("c3", "reg:dart:00888134", "(주)경주수출포장", host="alteogen.com", reg_no="1"),
        _row("c4", "name:kr:한빛유통", "한빛유통", host="alteogen.com"),
    ]
    out = {c.company_id: c for c in classify_group(rows, {})}
    assert out["c2"].action == RELATED  # 소유주 이름을 접두로 품음(자회사)
    assert out["c3"].action == REVIEW  # 등록처 키 행은 사람 판정
    assert out["c4"].action == AUTO_WRONG


# ── DB: 계획 → 적용 → 되돌리기 ─────────────────────────────────────────────────
def _seed(session: Session) -> None:
    session.add_all([
        DiscoveredCompanyRow(canonical_key="dom:yesform.com", name="주식회사 예스폼", country="KR",
                             domain="yesform.com"),
        DiscoveredCompanyRow(canonical_key="name:kr:향우종합관리", name="향우종합관리(주)",
                             country="KR", domain="yesform.com", source="nps"),
    ])
    session.flush()
    session.add_all([
        CompanyRow(id="c1", canonical_key="dom:yesform.com", name="주식회사 예스폼", country="KR",
                   homepage="https://yesform.com", site_alive=True, is_active=True),
        CompanyRow(id="c2", canonical_key="name:kr:향우종합관리", name="향우종합관리(주)",
                   country="KR", homepage="https://www.yesform.com/about", site_alive=True,
                   is_active=True),
    ])
    session.flush()
    for cid, typ, val in (
        ("c2", "email", "ir@yesform.com"), ("c2", "phone", "02-123-4567"),
        ("c2", "form", "https://yesform.com/contact"), ("c2", "email", "ceo@other.co.kr"),
        ("c1", "email", "info@yesform.com"),
    ):
        k = contact_id_for(cid, typ, val)
        session.add(ContactRow(id=k, company_id=cid, type=typ, value=val, role="ir"))
        session.flush()
        if typ == "email":
            session.add(EmailValidationRow(contact_id=k, status="valid", mx=True))
    enqueue_email_review(session, "c2", ["ir@yesform.com", "ceo@other.co.kr"])
    rq = session.get(ReviewQueueRow, review_id_for("c2", "email"))
    rq.status, rq.assignee, rq.selected, rq.selected_by_human = "confirmed", "kim", "ir@yesform.com", True
    rq.reviewed_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    session.flush()


def test_plan_apply_rollback_roundtrip(session: Session) -> None:
    _seed(session)
    plan = build_plan(session, now=lambda: datetime(2026, 9, 16, tzinfo=timezone.utc))
    assert plan.groups == 1 and plan.by_action == {OWNER: 1, AUTO_WRONG: 1}
    wrong = next(r for r in plan.rows if r.action == AUTO_WRONG)
    assert wrong.company_id == "c2"
    assert {c["value"] for c in wrong.before["contacts"]} == {
        "ir@yesform.com", "https://yesform.com/contact",
    }
    assert wrong.before["queue"]["status"] == "confirmed"
    # JSON 왕복(CLI 경로와 동일).
    plan = ConflictPlan.model_validate_json(plan.model_dump_json())
    wrong = next(r for r in plan.rows if r.action == AUTO_WRONG)

    stamp = datetime(2026, 9, 16, tzinfo=timezone.utc)
    assert apply_row(session, wrong, now=stamp) == "applied"
    co = session.get(CompanyRow, "c2")
    assert co is not None and co.homepage is None and co.site_alive is False and co.is_active is True
    assert session.get(DiscoveredCompanyRow, "name:kr:향우종합관리").domain is None
    left = {c.value for c in session.query(ContactRow).filter_by(company_id="c2")}
    assert left == {"02-123-4567", "ceo@other.co.kr"}  # 그 host 의 이메일·폼만 삭제, 전화 보존
    gone = contact_id_for("c2", "email", "ir@yesform.com")
    assert session.get(EmailValidationRow, gone) is None
    rq = session.get(ReviewQueueRow, review_id_for("c2", "email"))
    assert rq.status == "pending" and rq.assignee is None and rq.selected == "ceo@other.co.kr"
    assert json.loads(rq.candidates) == ["ceo@other.co.kr"]
    audit = session.query(ReviewAuditRow).filter_by(review_id=rq.id, action="domain_reset").one()
    assert audit.homepage_before == "https://www.yesform.com/about"
    assert session.get(CompanyRow, "c1").homepage == "https://yesform.com"  # 소유주는 그대로

    assert rollback_row(session, wrong) == "restored"
    co = session.get(CompanyRow, "c2")
    assert co.homepage == "https://www.yesform.com/about" and co.site_alive is True
    assert session.get(DiscoveredCompanyRow, "name:kr:향우종합관리").domain == "yesform.com"
    assert {c.value for c in session.query(ContactRow).filter_by(company_id="c2")} == {
        "ir@yesform.com", "02-123-4567", "https://yesform.com/contact", "ceo@other.co.kr",
    }
    assert session.get(EmailValidationRow, gone).status == "valid"
    rq = session.get(ReviewQueueRow, review_id_for("c2", "email"))
    assert rq.status == "confirmed" and rq.assignee == "kim" and rq.selected == "ir@yesform.com"
    assert rq.selected_by_human is True
    assert json.loads(rq.candidates) == ["ir@yesform.com", "ceo@other.co.kr"]


def test_apply_skips_when_homepage_changed_since_plan(session: Session) -> None:
    _seed(session)
    plan = build_plan(session)
    wrong = next(r for r in plan.rows if r.action == AUTO_WRONG)
    session.get(CompanyRow, "c2").homepage = "https://hyangwoo.co.kr"  # 그 사이 사람이 고침
    session.flush()
    assert apply_row(session, wrong, now=datetime(2026, 9, 16, tzinfo=timezone.utc)) == "stale"
    assert session.query(ContactRow).filter_by(company_id="c2").count() == 4


def test_groups_exclude_same_name_only(session: Session) -> None:
    session.add_all([
        DiscoveredCompanyRow(canonical_key="dom:a.com", name="에이스", country="KR", domain="a.com"),
        DiscoveredCompanyRow(canonical_key="name:kr:에이스", name="(주)에이스", country="KR", domain="a.com"),
    ])
    session.flush()
    session.add_all([
        CompanyRow(id="c1", canonical_key="dom:a.com", name="에이스", country="KR", homepage="https://a.com"),
        CompanyRow(id="c2", canonical_key="name:kr:에이스", name="(주)에이스", country="KR",
                   homepage="https://a.com"),
    ])
    session.flush()
    assert load_conflict_groups(session) == {}  # 이름이 같으면 dedup 몫


def test_apply_deletes_host_contacts_added_after_plan(session: Session) -> None:
    _seed(session)
    plan = build_plan(session)
    wrong = next(r for r in plan.rows if r.action == AUTO_WRONG)
    late = contact_id_for("c2", "email", "late@yesform.com")  # 계획 이후 크롤이 추가한 같은 host 이메일
    session.add(ContactRow(id=late, company_id="c2", type="email", value="late@yesform.com", role="ir"))
    session.flush()
    assert apply_row(session, wrong, now=datetime(2026, 9, 16, tzinfo=timezone.utc)) == "applied"
    assert session.get(ContactRow, late) is None


def test_rollback_keeps_new_homepage_and_refuses_after_human_rejudged(session: Session) -> None:
    _seed(session)
    plan = build_plan(session)
    wrong = next(r for r in plan.rows if r.action == AUTO_WRONG)
    stamp = datetime(2026, 9, 16, tzinfo=timezone.utc)
    assert apply_row(session, wrong, now=stamp) == "applied"
    # 재해석이 새 홈페이지를 채우고 살아있다고 판정한 뒤라면 홈페이지·site_alive 를 덮지 않는다.
    co = session.get(CompanyRow, "c2")
    co.homepage, co.site_alive = "https://hyangwoo.co.kr", True
    session.flush()
    assert rollback_row(session, wrong) == "restored"
    co = session.get(CompanyRow, "c2")
    assert co.homepage == "https://hyangwoo.co.kr" and co.site_alive is True
    assert session.get(DiscoveredCompanyRow, "name:kr:향우종합관리").domain == "yesform.com"
    # 사람이 다시 판정한 큐는 되돌리기가 거부한다.
    rq = session.get(ReviewQueueRow, review_id_for("c2", "email"))
    rq.status, rq.assignee = "confirmed", "lee"
    session.flush()
    assert rollback_row(session, wrong) == "stale"
    assert session.get(ReviewQueueRow, review_id_for("c2", "email")).assignee == "lee"


def test_mixed_script_parenthetical_is_not_latin_evidence_but_multi_token_title_is() -> None:
    rows = [
        _row("c1", "name:kr:동일상사", "동일상사 (APPLE)", host="apple.com"),
        _row("c2", "name:kr:애플코리아", "애플코리아", host="apple.com"),
        _row("c3", "name:us:alphamotors", "Alpha Motors", host="alphamotors.com", country="US"),
    ]
    out = {c.company_id: c for c in classify_group([rows[0], rows[1]], {})}
    assert out["c1"].evidence == [] and out["c1"].action == REVIEW  # 괄호 병기는 근거 아님 → 소유주 없음
    titles = {"alphamotors.com": "Alpha Motors | Official"}
    out = {c.company_id: c for c in classify_group([rows[2]], {}, titles)}
    assert out["c3"].evidence == ["latin_name", "title"]  # 다중 토큰은 연속 부분열로 판정


def test_related_includes_region_prefixed_branch() -> None:
    rows = [
        _row("c1", "dom:kepco.co.kr", "한국전력공사", host="kepco.co.kr"),
        _row("c2", "name:kr:성남한국전력공사지점", "성남한국전력공사지점", host="kepco.co.kr"),
    ]
    out = {c.company_id: c for c in classify_group(rows, {})}
    assert out["c2"].action == RELATED


def test_audited_hosts_uses_latest_change_only(session: Session) -> None:
    from leadcrawler.dedup_resolve.domain_conflict import audited_hosts

    session.add(DiscoveredCompanyRow(canonical_key="dom:a.com", name="에이", country="KR", domain="a.com"))
    session.flush()
    session.add(CompanyRow(id="c1", canonical_key="dom:a.com", name="에이", country="KR", homepage="https://b.com"))
    session.flush()
    enqueue_email_review(session, "c1", [])
    session.flush()  # audit FK 대상(큐 행) 먼저
    rid = review_id_for("c1", "email")
    session.add_all([
        ReviewAuditRow(id="a1", review_id=rid, actor_username="kim", action="confirmed",
                       homepage_after="https://a.com", at=datetime(2026, 9, 1, tzinfo=timezone.utc)),
        ReviewAuditRow(id="a2", review_id=rid, actor_username="kim", action="confirmed",
                       homepage_after="https://b.com", at=datetime(2026, 9, 10, tzinfo=timezone.utc)),
    ])
    session.flush()
    assert audited_hosts(session, ["c1"]) == {"c1": {"b.com"}}


def test_apply_is_stale_when_queue_rejudged_after_plan(session: Session) -> None:
    _seed(session)
    plan = build_plan(session)
    wrong = next(r for r in plan.rows if r.action == AUTO_WRONG)
    rq = session.get(ReviewQueueRow, review_id_for("c2", "email"))
    rq.assignee = "lee"  # 계획 이후 다른 검수자가 다시 판정
    session.flush()
    assert apply_row(session, wrong, now=datetime(2026, 9, 16, tzinfo=timezone.utc)) == "stale"
    assert session.get(CompanyRow, "c2").homepage == "https://www.yesform.com/about"
