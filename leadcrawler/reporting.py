"""운영 자동 리포팅 — 크롤러 통계·git 활동을 모아 Notion 서식을 자동 생성한다.

PO 요구: 일일보고·데일리스크럼·현황은 **사람이 손으로 쓰지 않는다**. 이 모듈이
파이프라인 산출(:class:`CompanyLead` 목록)과 git 커밋 로그를 집계해, Notion 에
기입할 :class:`DailyReport`/:class:`ScrumEntry` 를 자동으로 만든다(`--done`/`--next`
수기 입력 제거). 집계 함수는 순수·결정적이라 네트워크 없이 단위 테스트가 가능하다.
"""

from __future__ import annotations

import subprocess
from collections import Counter

from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .integrations.notion import DailyReport, NotionReporter, ScrumEntry, StatusTask
from .logging import get_logger
from .models import CompanyLead, ValidationStatus

log = get_logger("reporting")


class LeadStats(BaseModel):
    """:class:`CompanyLead` 목록을 집계한 일일 운영 지표."""

    total: int = 0  # 처리된 리드 총수
    active: int = 0  # 실존(active) 회사 수
    with_email: int = 0  # 채택 이메일이 있는 리드
    with_phone: int = 0
    with_form: int = 0  # 이메일 없이 문의폼만
    email_valid: int = 0
    email_risky: int = 0
    email_invalid: int = 0
    smtp_confirmed: int = 0  # SMTP RCPT 로 메일박스 수신 확정
    by_method: dict[str, int] = Field(default_factory=dict)  # 이메일 추출 출처별(escalation 티어)
    by_country: dict[str, int] = Field(default_factory=dict)


def summarize_leads(leads: list[CompanyLead]) -> LeadStats:
    """리드 목록을 운영 지표로 집계한다(순수·결정적)."""
    methods: Counter[str] = Counter()
    countries: Counter[str] = Counter()
    stats = LeadStats(total=len(leads))
    for lead in leads:
        if lead.company.is_active:
            stats.active += 1
        countries[lead.company.country or "?"] += 1
        if lead.email is not None:
            stats.with_email += 1
            methods[lead.email.extract_method.value] += 1
        elif lead.form is not None:
            stats.with_form += 1
        if lead.phone is not None:
            stats.with_phone += 1
        status = lead.email_validation.status
        if status is ValidationStatus.VALID:
            stats.email_valid += 1
        elif status is ValidationStatus.RISKY:
            stats.email_risky += 1
        elif status is ValidationStatus.INVALID:
            stats.email_invalid += 1
        if lead.email_validation.smtp is True:
            stats.smtp_confirmed += 1
    stats.by_method = dict(methods)
    stats.by_country = dict(countries)
    return stats


def git_commits_since(date: str, *, max_count: int = 50) -> list[str]:
    """``date``(YYYY-MM-DD, UTC) 하루치 커밋 제목 목록을 반환한다.

    git 이 없거나 저장소가 아니어도 빈 목록으로 graceful 폴백한다(리포팅은 통계만으로도
    유효해야 한다). 보고 일자는 UTC 로 산정되므로 ``--since/--until`` 도 명시적 UTC
    경계(00:00:00~23:59:59+00:00)로 고정해, 로컬 타임존에 따른 자정 전후 누락/혼입을 막는다.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "log",
                f"--since={date}T00:00:00+00:00",
                f"--until={date}T23:59:59+00:00",
                f"--max-count={max_count}",
                "--pretty=%s",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",  # 한글 커밋 메시지 — OS 로케일(cp949) 의존 회피
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # git 미설치·타임아웃 등
        log.info("reporting.git_unavailable", error=str(exc))
        return []
    if proc.returncode != 0:
        log.info("reporting.git_failed", code=proc.returncode)
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _format_done(stats: LeadStats, commits: list[str]) -> str:
    """통계+커밋을 '한 일' 본문으로 자동 구성한다."""
    lines = [
        f"리드 {stats.total}건 처리(실존 {stats.active})",
        f"이메일 확보 {stats.with_email}(검증 valid {stats.email_valid}/"
        f"risky {stats.email_risky}/invalid {stats.email_invalid}, "
        f"SMTP확정 {stats.smtp_confirmed})",
        f"전화 {stats.with_phone} · 문의폼만 {stats.with_form}",
    ]
    if stats.by_method:
        tiers = ", ".join(f"{k}={v}" for k, v in sorted(stats.by_method.items()))
        lines.append(f"이메일 출처: {tiers}")
    if stats.by_country:
        cty = ", ".join(f"{k}={v}" for k, v in sorted(stats.by_country.items()))
        lines.append(f"국가: {cty}")
    if commits:
        lines.append("커밋:")
        lines.extend(f"- {c}" for c in commits)
    return "\n".join(lines)


def _format_status(stats: LeadStats) -> str:
    """집계 기반 진행 상태(이메일 확보율이 0이면 '점검')."""
    if stats.total == 0:
        return "점검"  # 산출 0 — 발견/네트워크 이상 의심
    if stats.with_email == 0:
        return "주의"
    return "정상"


def build_daily_report(
    date: str,
    stats: LeadStats,
    commits: list[str],
    *,
    milestone: str | None = None,
    next_plan: str = "",
) -> DailyReport:
    """통계·커밋으로 일일 보고서를 자동 구성한다(수기 입력 없음)."""
    return DailyReport(
        date=date,
        milestone=milestone,
        done=_format_done(stats, commits),
        next=next_plan,
        issues="없음" if _format_status(stats) == "정상" else "산출 지표 점검 필요",
        status=_format_status(stats),
    )


def build_status_task(
    date: str, stats: LeadStats, *, milestone: str | None = None
) -> StatusTask:
    """그날 크롤 운영 결과를 현황보드 1행(완료 태스크)으로 자동 구성한다.

    현황보드는 작업 추적용이라, 일일 운영 트레일을 'Done' 태스크로 남긴다 — 매일
    누적되면 운영 이력이 된다(사람 수기 입력 없음).
    """
    note = (
        f"리드 {stats.total}(실존 {stats.active}) · 이메일 {stats.with_email}"
        f"(valid {stats.email_valid}/SMTP {stats.smtp_confirmed}) · "
        f"전화 {stats.with_phone} · 폼 {stats.with_form}"
    )
    return StatusTask(
        task=f"{date} 크롤 운영",
        milestone=milestone,
        status="Done" if stats.total else "점검",
        priority="Mid",
        owner="시스템(자동)",
        note=note,
    )


def build_scrum(
    date: str, stats: LeadStats, commits: list[str], *, next_plan: str = ""
) -> ScrumEntry:
    """통계·커밋으로 데일리 스크럼을 자동 구성한다.

    ``next_plan`` 이 주어지면 '오늘 할 일'로 우선 사용한다(없으면 통계 기반 기본 문구).
    """
    yesterday = "; ".join(commits[:5]) if commits else f"리드 {stats.total}건 처리"
    return ScrumEntry(
        date=date,
        yesterday=yesterday,
        today=next_plan or f"리드 {stats.with_email}건 이메일 검증·후속 보강",
        blocker="없음" if stats.total else "발견 산출 0 — 소스 점검",
    )


def auto_report(
    leads: list[CompanyLead],
    *,
    date: str,
    settings: Settings | None = None,
    milestone: str | None = None,
    next_plan: str = "",
    commits: list[str] | None = None,
) -> dict[str, dict]:
    """리드 통계+git 활동을 모아 Notion 일일보고·스크럼·현황을 자동 기입한다.

    집계(``summarize_leads``)와 본문 생성은 순수·결정적이다. ``commits`` 를 주면 그대로
    쓰고, 생략하면 ``git_commits_since`` 로 실 git 상태를 읽는다(이 경우 본문은 git 이력에
    의존). **전송 자체**는 ``dry_run`` 또는 토큰 부재 시 :class:`NotionReporter` 가 네트워크
    없이 payload 만 반환하므로 결정적이다. 일일보고·스크럼·현황보드 3종 payload 를 돌려준다.
    """
    settings = settings or get_settings()
    stats = summarize_leads(leads)
    if commits is None:
        commits = git_commits_since(date)
    reporter = NotionReporter(settings)
    daily = reporter.post_daily_report(
        build_daily_report(date, stats, commits, milestone=milestone, next_plan=next_plan)
    )
    scrum = reporter.post_scrum(build_scrum(date, stats, commits, next_plan=next_plan))
    status = reporter.post_status(build_status_task(date, stats, milestone=milestone))
    log.info(
        "reporting.auto",
        date=date,
        total=stats.total,
        with_email=stats.with_email,
        sent=reporter.enabled,
    )
    return {"daily": daily, "scrum": scrum, "status": status}
