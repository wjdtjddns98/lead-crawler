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


def test_preview_counts_without_sending(db_settings, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(outreach, "send_one", lambda *a, **k: calls.append(1))
    s = _settings(email_send_enabled=True)
    with session_scope(s) as sess:
        p = outreach.preview(s, sess, countries=["KR"])
    assert p["recipients"] == 2 and p["enabled"] is True and not calls
    assert set(p["sample"]) <= {"ir@a.co.kr", "ir@b.co.kr"}
