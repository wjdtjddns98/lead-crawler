"""아웃리치 이메일 발송 — 확정(confirmed) 큐 대상 전체발송.

설계 원칙(안전 우선): 발송은 외부행위라 ``email_send_enabled`` 가 켜져야만 실제로
나간다(꺼져 있으면 수신 미리보기만, 네트워크 0). 수신주소당 1통(재발송 방지 —
``email_send_log`` 의 status='sent' 면 제외), 일일 상한·발송 간 레이트리밋, per-수신
성공/실패 로그로 책임추적한다. 제목·본문·발신표시명은 호출부(웹앱 폼)가 사람 입력으로
넘긴다. From 주소는 인증 계정(``smtp_send_user``)으로 고정(표시명만 가변).
"""

from __future__ import annotations

import hashlib
import smtplib
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from email.message import EmailMessage

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .logging import get_logger
from .schema import CompanyRow, EmailSendLogRow, ReviewQueueRow
from .sources.countries import country_match_set
from .storage.review import CONFIRMED, candidate_values_of, effective_selected

log = get_logger("outreach")


def _send_id(email: str) -> str:
    """수신주소에서 결정적 PK(재발송 방지 — 주소당 1행)."""
    return "e_" + hashlib.sha1(email.encode("utf-8")).hexdigest()[:38]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def send_one(
    settings: Settings, *, to: str, subject: str, body: str, from_display: str = ""
) -> None:
    """SMTP(STARTTLS+로그인)로 1통 발송한다. 실패 시 예외를 던진다.

    From 은 인증 계정(``smtp_send_user``)으로 고정하고, ``from_display`` 가 있으면
    표시명만 붙인다(임의 From 은 Gmail 이 거부/스팸 처리하므로).
    """
    sender = settings.smtp_send_user
    msg = EmailMessage()
    msg["From"] = f"{from_display} <{sender}>" if from_display.strip() else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(
        settings.smtp_send_host, settings.smtp_send_port, timeout=settings.smtp_timeout
    ) as server:
        server.starttls()
        server.login(sender, settings.smtp_send_password)
        server.send_message(msg)


def recipients(
    session: Session, *, countries: Sequence[str] = (), industries: Sequence[str] = ()
) -> list[tuple[str, str]]:
    """확정 큐의 선택 이메일 (company_id, email) 목록 — 주소 dedup + 이미 발송분 제외.

    국가는 별칭·대소문자 무시 매칭('KR'↔'대한민국'), 업종은 대소문자 무시.
    """
    stmt = (
        select(ReviewQueueRow, CompanyRow)
        .join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
        .where(ReviewQueueRow.status == CONFIRMED)
    )
    if countries:
        stmt = stmt.where(func.lower(CompanyRow.country).in_(country_match_set(countries)))
    if industries:
        stmt = stmt.where(func.lower(CompanyRow.industry).in_({i.strip().lower() for i in industries}))

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rq, company in session.execute(stmt).all():
        email = effective_selected(rq.selected, candidate_values_of(rq))
        if not email or email in seen:
            continue
        seen.add(email)
        out.append((company.id, email))

    if out:  # 이미 발송 성공(status='sent')한 주소는 재발송 제외.
        already = set(
            session.scalars(
                select(EmailSendLogRow.email).where(
                    EmailSendLogRow.email.in_([e for _, e in out]),
                    EmailSendLogRow.status == "sent",
                )
            ).all()
        )
        out = [(cid, e) for cid, e in out if e not in already]
    return out


def _today_sent_count(session: Session, now: datetime) -> int:
    """오늘(UTC) 실발송 성공 건수 — 일일 상한 계산용."""
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int(
        session.scalar(
            select(func.count())
            .select_from(EmailSendLogRow)
            .where(EmailSendLogRow.status == "sent", EmailSendLogRow.sent_at >= start)
        )
        or 0
    )


def preview(
    settings: Settings,
    session: Session,
    *,
    countries: Sequence[str] = (),
    industries: Sequence[str] = (),
    now: datetime | None = None,
    sample: int = 10,
) -> dict:
    """발송 전 미리보기 — 수신 N명·일일 잔여 상한·표본(실발송 없음, 네트워크 0)."""
    now = now or _utcnow()
    recips = recipients(session, countries=countries, industries=industries)
    cap = max(0, settings.email_send_daily_cap)
    remaining = max(0, cap - _today_sent_count(session, now))
    return {
        "recipients": len(recips),
        "enabled": bool(settings.email_send_enabled),
        "daily_cap": cap,
        "remaining_today": remaining,
        "sender": settings.smtp_send_user,
        "sample": [e for _, e in recips[:sample]],
    }


def _log_send(
    session: Session,
    *,
    email: str,
    company_id: str,
    subject: str,
    status: str,
    error: str | None,
    sent_by: str | None,
    now: datetime,
) -> None:
    """발송 결과를 주소당 1행으로 멱등 기록(재발송 방지 + 책임추적)."""
    rid = _send_id(email)
    row = session.get(EmailSendLogRow, rid)
    if row is None:
        row = EmailSendLogRow(id=rid, email=email, company_id=company_id)
        session.add(row)
    row.company_id = company_id
    row.subject = subject[:512]
    row.status = status
    row.error = error
    row.sent_by = sent_by
    row.sent_at = now


def send_campaign(
    settings: Settings,
    session: Session,
    *,
    subject: str,
    body: str,
    from_display: str = "",
    countries: Sequence[str] = (),
    industries: Sequence[str] = (),
    sent_by: str | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """확정큐 수신자에게 발송한다(안전 게이트·상한·레이트리밋·재발송방지·로그).

    ``email_send_enabled`` 가 꺼져 있으면 **실발송·로그 없이** dry-run 요약만 반환한다.
    켜져 있으면 일일 잔여 상한까지만 보내고 발송 간 ``email_send_min_interval`` 만큼 쉰다.
    """
    now = now or _utcnow()
    recips = recipients(session, countries=countries, industries=industries)

    if not settings.email_send_enabled:  # 안전 게이트 — 실발송 차단(미리보기 동치).
        log.info("outreach.dry_run", recipients=len(recips))
        return {
            "dry_run": True,
            "recipients": len(recips),
            "attempted": 0,
            "sent": 0,
            "failed": 0,
            "capped": 0,
        }

    cap = max(0, settings.email_send_daily_cap)
    remaining = max(0, cap - _today_sent_count(session, now))
    target = recips[:remaining]
    sent = failed = 0
    for i, (company_id, email) in enumerate(target):
        try:
            send_one(settings, to=email, subject=subject, body=body, from_display=from_display)
            _log_send(session, email=email, company_id=company_id, subject=subject,
                      status="sent", error=None, sent_by=sent_by, now=now)
            sent += 1
        except Exception as exc:  # 한 통 실패가 캠페인 전체를 막지 않게(로그 후 계속).
            _log_send(session, email=email, company_id=company_id, subject=subject,
                      status="failed", error=str(exc)[:500], sent_by=sent_by, now=now)
            failed += 1
            log.info("outreach.send_error", email=email, err=str(exc))
        if i < len(target) - 1 and settings.email_send_min_interval > 0:
            sleep(settings.email_send_min_interval)  # 레이트리밋(계정 차단 방지).
    session.flush()
    log.info("outreach.sent", sent=sent, failed=failed, capped=len(recips) - len(target))
    return {
        "dry_run": False,
        "recipients": len(recips),
        "attempted": len(target),
        "sent": sent,
        "failed": failed,
        "capped": len(recips) - len(target),  # 일일 상한 초과로 미발송.
    }
