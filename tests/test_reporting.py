"""운영 자동 리포팅 테스트 — 집계·빌드·dry_run payload(네트워크 없음)."""

from __future__ import annotations

from leadcrawler.config import get_settings
from leadcrawler.models import (
    Company,
    CompanyLead,
    Contact,
    ContactType,
    EmailValidation,
    ExtractMethod,
    ValidationStatus,
)
import subprocess

import pytest

from leadcrawler.reporting import (
    auto_report,
    build_daily_report,
    build_scrum,
    build_status_task,
    git_commits_since,
    summarize_leads,
)


def _lead(
    *,
    country: str = "KR",
    active: bool = True,
    email: str | None = None,
    method: ExtractMethod = ExtractMethod.STATIC,
    status: ValidationStatus = ValidationStatus.UNKNOWN,
    smtp: bool | None = None,
    phone: bool = False,
    form: bool = False,
) -> CompanyLead:
    """테스트용 :class:`CompanyLead` 한 건을 구성한다."""
    company = Company(canonical_key=f"k:{country}:{email}", name="테스트", country=country, is_active=active)
    return CompanyLead(
        company=company,
        email=Contact(type=ContactType.EMAIL, value=email, extract_method=method) if email else None,
        phone=Contact(type=ContactType.PHONE, value="02-000") if phone else None,
        form=Contact(type=ContactType.FORM, value="https://x/contact") if form else None,
        email_validation=EmailValidation(status=status, smtp=smtp),
    )


def test_summarize_counts() -> None:
    leads = [
        _lead(email="ir@a.com", method=ExtractMethod.STATIC, status=ValidationStatus.VALID, smtp=True),
        _lead(email="info@b.com", method=ExtractMethod.API, status=ValidationStatus.RISKY, phone=True),
        _lead(country="US", active=False, form=True, status=ValidationStatus.UNKNOWN),
    ]
    stats = summarize_leads(leads)
    assert stats.total == 3
    assert stats.active == 2
    assert stats.with_email == 2
    assert stats.with_form == 1
    assert stats.with_phone == 1
    assert stats.email_valid == 1
    assert stats.email_risky == 1
    assert stats.smtp_confirmed == 1
    assert stats.by_method == {"static": 1, "api": 1}
    assert stats.by_country == {"KR": 2, "US": 1}


def test_summarize_empty() -> None:
    stats = summarize_leads([])
    assert stats.total == 0
    assert stats.by_method == {}


def test_build_daily_report_auto_filled() -> None:
    stats = summarize_leads([_lead(email="ir@a.com", status=ValidationStatus.VALID)])
    report = build_daily_report("2026-06-22", stats, ["feat: x", "fix: y"], milestone="M3")
    assert report.date == "2026-06-22"
    assert report.milestone == "M3"
    assert report.author == "시스템(자동)"  # 수기 작성자 아님
    assert "리드 1건" in report.done
    assert "feat: x" in report.done  # git 활동 자동 반영
    assert report.status == "정상"


def test_build_daily_report_status_flags() -> None:
    assert build_daily_report("2026-06-22", summarize_leads([]), []).status == "점검"
    no_email = summarize_leads([_lead()])  # email=None
    assert build_daily_report("2026-06-22", no_email, []).status == "주의"


def test_build_scrum() -> None:
    stats = summarize_leads([_lead(email="ir@a.com")])
    scrum = build_scrum("2026-06-22", stats, ["feat: x"])
    assert scrum.date == "2026-06-22"
    assert "feat: x" in scrum.yesterday
    assert scrum.blocker == "없음"
    empty = build_scrum("2026-06-22", summarize_leads([]), [])
    assert "소스 점검" in empty.blocker


def test_build_scrum_uses_next_plan() -> None:
    stats = summarize_leads([_lead(email="ir@a.com")])
    scrum = build_scrum("2026-06-22", stats, [], next_plan="웹앱 착수")
    assert scrum.today == "웹앱 착수"  # next_plan 우선


def test_build_status_task() -> None:
    stats = summarize_leads([_lead(email="ir@a.com", status=ValidationStatus.VALID)])
    task = build_status_task("2026-06-22", stats, milestone="M3")
    assert task.task == "2026-06-22 크롤 운영"
    assert task.status == "Done"
    assert task.owner == "시스템(자동)"
    assert "리드 1" in task.note
    assert build_status_task("2026-06-22", summarize_leads([])).status == "점검"


def test_git_commits_since_returns_list() -> None:
    # 실 저장소에서 동작하나, 형식만 검증(graceful: 실패해도 list).
    out = git_commits_since("2000-01-01", max_count=1)
    assert isinstance(out, list)


def test_git_commits_since_git_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **k: object) -> None:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert git_commits_since("2026-06-22") == []


def test_git_commits_since_non_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    assert git_commits_since("2026-06-22") == []


def test_git_commits_since_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(subprocess, "run", _timeout)
    assert git_commits_since("2026-06-22") == []


def test_git_commits_since_strips_blank_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        returncode = 0
        stdout = "feat: a\n\n  fix: b  \n\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    assert git_commits_since("2026-06-22") == ["feat: a", "fix: b"]


def test_auto_report_dry_run_payloads() -> None:
    leads = [_lead(email="ir@a.com", status=ValidationStatus.VALID)]
    # commits 주입 → git 상태와 무관하게 결정적
    result = auto_report(
        leads, date="2026-06-22", settings=get_settings(), milestone="M3", commits=["feat: x"]
    )
    # dry_run → 네트워크 없이 payload 반환, 3종 보드 전부
    daily = result["daily"]
    assert daily["parent"]["database_id"] == get_settings().notion_daily_db
    assert daily["properties"]["제목"]["title"][0]["text"]["content"] == "2026-06-22 일일 보고"
    assert result["scrum"]["properties"]["제목"]["title"][0]["text"]["content"] == "2026-06-22 스크럼"
    status = result["status"]
    assert status["parent"]["database_id"] == get_settings().notion_status_db
    assert status["properties"]["태스크"]["title"][0]["text"]["content"] == "2026-06-22 크롤 운영"
