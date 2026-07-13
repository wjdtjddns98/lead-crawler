"""아웃리치 발송 테스트 — dry-run 게이트·실발송 로그·재발송 dedup·일일상한·필터.

실제 SMTP 는 conftest 가 차단하므로, 발송은 ``outreach.send_one`` 을 monkeypatch 한다.
"""

from __future__ import annotations

import pytest

from leadcrawler import outreach
from leadcrawler.config import get_settings
from leadcrawler.models import (
    Company,
    CompanyLead,
    Contact,
    ContactType,
    EmailRole,
    EmailValidation,
    ValidationStatus,
)
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import company_id_for, save_lead
from leadcrawler.storage.review import CONFIRMED, review_id_for, set_review_status


def _seed_confirmed(settings, *, name: str, country: str, industry: str, email: str) -> None:
    domain = email.split("@", 1)[1]
    lead = CompanyLead(
        company=Company(
            canonical_key=f"dom:{domain}", name=name, country=country, industry=industry,
            domain=domain, homepage=f"https://{domain}", is_active=True, site_alive=True,
        ),
        email=Contact(type=ContactType.EMAIL, value=email, role=EmailRole.IR),
        email_validation=EmailValidation(status=ValidationStatus.VALID, mx=True),
    )
    with session_scope(settings) as s:
        save_lead(s, lead, source="test")
        cid = company_id_for(lead.company.canonical_key)
        set_review_status(s, review_id_for(cid, "email"), CONFIRMED, selected=email)


def _settings(**over):
    # conftest 의 격리 DB URL 을 보존한 채 발송 설정만 덮어쓴다.
    base = {"email_send_min_interval": 0.0, **over}
    return get_settings().model_copy(update=base)


@pytest.fixture
def db_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/send.db")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings)
    _seed_confirmed(settings, name="A", country="KR", industry="건설", email="ir@a.co.kr")
    _seed_confirmed(settings, name="B", country="KR", industry="건설", email="ir@b.co.kr")
    _seed_confirmed(settings, name="C", country="US", industry="반도체", email="ir@c.com")
    return settings


def test_dry_run_does_not_send_or_log(db_settings, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(outreach, "send_one", lambda *a, **k: calls.append(k.get("to")))
    s = _settings(email_send_enabled=False)
    with session_scope(s) as sess:
        result = outreach.send_campaign(s, sess, subject="제목", body="본문")
    assert result["dry_run"] is True and result["sent"] == 0
    assert result["recipients"] == 3 and not calls  # 실발송 0.
    # dry-run 은 로그도 남기지 않는다.
    from leadcrawler.schema import EmailSendLogRow
    with session_scope(s) as sess:
        assert sess.query(EmailSendLogRow).count() == 0


def test_enabled_sends_logs_and_dedups(db_settings, monkeypatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        outreach, "send_one",
        lambda settings, *, to, subject, body, from_display="": sent.append(to),
    )
    s = _settings(email_send_enabled=True)
    with session_scope(s) as sess:
        r1 = outreach.send_campaign(s, sess, subject="제목", body="본문", sleep=lambda _: None)
    assert r1["sent"] == 3 and r1["failed"] == 0
    assert set(sent) == {"ir@a.co.kr", "ir@b.co.kr", "ir@c.com"}
    # 재실행 — 이미 발송한 3건은 수신 목록에서 빠진다(재발송 방지).
    sent.clear()
    with session_scope(s) as sess:
        r2 = outreach.send_campaign(s, sess, subject="제목", body="본문", sleep=lambda _: None)
    assert r2["recipients"] == 0 and r2["sent"] == 0 and not sent


def test_country_industry_filter(db_settings, monkeypatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        outreach, "send_one",
        lambda settings, *, to, subject, body, from_display="": sent.append(to),
    )
    s = _settings(email_send_enabled=True)
    with session_scope(s) as sess:
        r = outreach.send_campaign(
            s, sess, subject="x", body="y", countries=["KR"], industries=["건설"],
            sleep=lambda _: None,
        )
    assert r["sent"] == 2 and set(sent) == {"ir@a.co.kr", "ir@b.co.kr"}  # US/반도체 제외.


def test_daily_cap_limits_send(db_settings, monkeypatch) -> None:
    monkeypatch.setattr(outreach, "send_one", lambda *a, **k: None)
    s = _settings(email_send_enabled=True, email_send_daily_cap=1)
    with session_scope(s) as sess:
        r = outreach.send_campaign(s, sess, subject="x", body="y", sleep=lambda _: None)
    assert r["sent"] == 1 and r["capped"] == 2  # 상한 1 → 1통, 나머지 2 미발송.


def test_failure_is_logged_and_continues(db_settings, monkeypatch) -> None:
    def _boom(settings, *, to, subject, body, from_display=""):
        if to == "ir@a.co.kr":
            raise RuntimeError("smtp refused")

    monkeypatch.setattr(outreach, "send_one", _boom)
    s = _settings(email_send_enabled=True)
    with session_scope(s) as sess:
        r = outreach.send_campaign(s, sess, subject="x", body="y", sleep=lambda _: None)
    assert r["failed"] == 1 and r["sent"] == 2  # 한 통 실패해도 나머지 진행.


def test_sends_persist_across_request_rollback(db_settings, monkeypatch) -> None:
    """발송 직후 격리 커밋 — 요청 세션이 이후 롤백돼도 send_log 가 살아남는다(중복발송 방지)."""
    from leadcrawler.schema import EmailSendLogRow
    from leadcrawler.storage.db import get_sessionmaker

    sent: list[str] = []
    monkeypatch.setattr(
        outreach, "send_one",
        lambda settings, *, to, subject, body, from_display="": sent.append(to),
    )
    s = _settings(email_send_enabled=True)
    # 요청 세션(get_db)을 모사: 발송 후 핸들러 예외처럼 rollback 한다.
    session = get_sessionmaker(s)()
    try:
        r = outreach.send_campaign(s, session, subject="제목", body="본문", sleep=lambda _: None)
        assert r["sent"] == 3
        session.rollback()  # 요청 트랜잭션 실패/롤백 모사 — 격리 커밋이라 발송기록은 보존돼야.
    finally:
        session.close()

    with session_scope(s) as v:  # 발송기록 3건이 디스크에 영속(롤백 무관).
        rows = v.query(EmailSendLogRow).filter(EmailSendLogRow.status == "sent").all()
    assert len(rows) == 3
    # 재실행 시 이미 발송분은 수신목록에서 제외 → 재발송 0(내구성의 실제 효과).
    sent.clear()
    with session_scope(s) as v:
        r2 = outreach.send_campaign(s, v, subject="제목", body="본문", sleep=lambda _: None)
    assert r2["recipients"] == 0 and not sent


def test_preview_counts_without_sending(db_settings, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(outreach, "send_one", lambda *a, **k: calls.append(1))
    s = _settings(email_send_enabled=True)
    with session_scope(s) as sess:
        p = outreach.preview(s, sess, countries=["KR"])
    assert p["recipients"] == 2 and p["enabled"] is True and not calls
    assert set(p["sample"]) <= {"ir@a.co.kr", "ir@b.co.kr"}


def test_reserve_send_blocks_duplicate_race(db_settings) -> None:
    """선점 원자성(전수리뷰) — 같은 주소의 두 번째 예약은 dup(중복 발송 창 제거)."""
    from datetime import datetime, timezone

    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    kw = dict(email="ir@a.co.kr", company_id="c1", subject="s", sent_by=None, cap=10, now=now)
    assert outreach._reserve_send(db_settings, **kw) == "reserved"
    assert outreach._reserve_send(db_settings, **kw) == "dup"  # 상대 캠페인 시점의 재예약.


def test_reserve_send_enforces_cap_and_stale_retry(db_settings) -> None:
    """상한은 예약(sending) 포함 계산, 좌초 예약은 stale 창 이후 재예약 허용."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    assert outreach._reserve_send(
        db_settings, email="ir@a.co.kr", company_id="c1", subject="s",
        sent_by=None, cap=1, now=now,
    ) == "reserved"
    # 예약 1건이 cap=1 을 소진 — 두 번째 수신자는 SMTP 전에 차단(상한 초과 발송 불가).
    assert outreach._reserve_send(
        db_settings, email="ir@b.co.kr", company_id="c2", subject="s",
        sent_by=None, cap=1, now=now,
    ) == "capped"
    # 좌초(sending 박제) 예약은 stale 창(_RESERVE_STALE_SEC) 이후 재예약된다.
    later = now + timedelta(seconds=outreach._RESERVE_STALE_SEC + 1)
    assert outreach._reserve_send(
        db_settings, email="ir@a.co.kr", company_id="c1", subject="s",
        sent_by=None, cap=10, now=later,
    ) == "reserved"


def test_send_campaign_skips_recipient_reserved_elsewhere(db_settings, monkeypatch) -> None:
    """다른 요청이 방금 선점한 수신자는 SMTP 없이 스킵(skipped 카운트)."""
    from datetime import datetime, timezone

    sent_to: list[str] = []
    monkeypatch.setattr(outreach, "send_one", lambda *_a, **k: sent_to.append(k["to"]))
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    # 경쟁 요청이 ir@a.co.kr 를 선점한 상황 재현.
    outreach._reserve_send(
        db_settings, email="ir@a.co.kr", company_id="cx", subject="s",
        sent_by=None, cap=10, now=now,
    )
    s = _settings(email_send_enabled=True, email_send_daily_cap=10,
                  database_url=db_settings.database_url)
    with session_scope(db_settings) as db:
        out = outreach.send_campaign(
            s, db, subject="s", body="b", countries=("KR",), now=now,
        )
    assert "ir@a.co.kr" not in sent_to  # 선점분 스킵.
    assert out["skipped"] == 1 and out["sent"] == 1  # ir@b.co.kr 만 발송.
