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
from email.generator import BytesGenerator
from io import BytesIO
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .logging import get_logger
from .schema import CompanyRow, EmailSendLogRow, ReviewQueueRow
from .sources.countries import country_match_set
from .storage.db import session_scope
from .storage.review import CONFIRMED, candidate_values_of, effective_selected

log = get_logger("outreach")


def _send_id(email: str) -> str:
    """수신주소에서 결정적 PK(재발송 방지 — 주소당 1행)."""
    return "e_" + hashlib.sha1(email.encode("utf-8")).hexdigest()[:38]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# "전달됐을 수 있음" 상태 묶음 — 재발송 제외·예약 dup·일일 상한 계산에 공통 적용.
# uncertain = DATA 전송 후 응답을 못 읽음(끊김/타임아웃): 수신 MTA 가 이미 큐잉했을 수 있어
# 자동 재시도(failed 재예약)에서 뺀다 — 재발송은 운영자가 확인 후 수동으로만:
#   조회  select email, error, sent_at from email_send_log where status='uncertain';
#   재개  update email_send_log set status='failed' where id=<id>;  (다음 캠페인이 재예약)
# ponytail: 관리 API/CLI 없음 — 건수가 쌓이면 admin 라우트로.
_DELIVERED = ("sent", "uncertain")


class SendUncertain(Exception):
    """발송 결과 불명 — 페이로드는 보냈으나 서버 응답 전에 연결이 끊기거나 타임아웃."""


def send_one(
    settings: Settings,
    *,
    to: str,
    subject: str,
    body: str,
    from_display: str = "",
    server: smtplib.SMTP | None = None,
) -> smtplib.SMTP:
    """SMTP(STARTTLS+로그인)로 1통 발송하고 **사용한 연결을 돌려준다**. 실패 시 예외.

    ``server`` 를 주면 그 연결로 보내고(캠페인 재사용 — 수신자마다 접속+TLS+로그인 왕복
    제거), 없으면 새로 접속·로그인한다. 연결 종료(``quit``)는 호출자 책임.
    From 은 인증 계정(``smtp_send_user``)으로 고정하고, ``from_display`` 가 있으면
    표시명만 붙인다(임의 From 은 Gmail 이 거부/스팸 처리하므로).
    """
    sender = settings.smtp_send_user
    msg = EmailMessage()
    msg["From"] = f"{from_display} <{sender}>" if from_display.strip() else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if server is None:
        server = smtplib.SMTP(
            settings.smtp_send_host, settings.smtp_send_port, timeout=settings.smtp_timeout
        )
        try:
            server.starttls()
            server.login(sender, settings.smtp_send_password)
        except Exception:  # TLS/로그인 실패 — 열린 소켓을 호출자가 못 받으므로 여기서 닫는다.
            server.close()
            raise
    # send_message() 대신 MAIL→RCPT→DATA 를 직접 밟는다 — 끊김이 **DATA 단계**에서 났을 때만
    # "불명"이다(MAIL/RCPT 단계 끊김은 페이로드 0바이트 = 확실한 미전송 → 그대로 실패·재시도).
    # smtplib 은 소켓 OSError 를 전부 SMTPServerDisconnected 로 감싸므로 그 하나만 본다.
    code, resp = server.mail(sender)
    if code != 250:
        _smtp_rset(server)
        raise smtplib.SMTPSenderRefused(code, resp, sender)
    code, resp = server.rcpt(to)
    if code not in (250, 251):
        _smtp_rset(server)
        raise smtplib.SMTPRecipientsRefused({to: (code, resp)})
    buf = BytesIO()
    BytesGenerator(buf, policy=msg.policy.clone(linesep="\r\n")).flatten(msg)
    try:
        code, resp = server.data(buf.getvalue())
    except smtplib.SMTPServerDisconnected as exc:
        # data() 는 페이로드 전체를 보낸 뒤 250 을 읽는다 — 그 사이 끊김은 수신 MTA 가 이미
        # 큐잉했을 수 있다. (354 응답 전 끊김도 여기 포함되는 잔여 한계 — 드물고 안전측.)
        raise SendUncertain(str(exc)) from exc
    if code != 250:  # 서버가 본문을 명시 거절 — 미전달 확정(재시도 가능).
        _smtp_rset(server)
        raise smtplib.SMTPDataError(code, resp)
    return server


def _smtp_rset(server: smtplib.SMTP) -> None:
    try:
        server.rset()
    except smtplib.SMTPServerDisconnected:
        pass


def _smtp_alive(server: smtplib.SMTP) -> bool:
    """유휴(레이트리밋 sleep) 중 서버가 끊었는지 — NOOP 1왕복(접속+TLS+로그인보다 훨씬 싸다)."""
    try:
        return server.noop()[0] == 250
    except Exception:
        return False


def _smtp_quit(server: smtplib.SMTP | None) -> None:
    if server is not None:
        try:
            server.quit()
        except Exception:  # 이미 끊긴 연결 — 정리 실패는 무시.
            pass


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

    if out:  # 이미 발송 성공(sent)·결과 불명(uncertain)인 주소는 재발송 제외.
        already = set(
            session.scalars(
                select(EmailSendLogRow.email).where(
                    EmailSendLogRow.email.in_([e for _, e in out]),
                    EmailSendLogRow.status.in_(_DELIVERED),
                )
            ).all()
        )
        out = [(cid, e) for cid, e in out if e not in already]
    return out


def _today_used_count(session: Session, now: datetime) -> int:
    """오늘(UTC) 상한 사용량 = 실발송(sent)·결과 불명(uncertain) + **신선한** 예약(sending).

    좌초 예약(sending 이 _RESERVE_STALE_SEC 초과)은 제외한다 — 크래시 박제 1건이
    그날 상한을 영구히 갉아먹는 것 방지(교차리뷰 MED). preview 와 예약검사가 이
    함수를 공유해 '미리보기 잔여 ≠ 실제 발송 가능' 불일치를 없앤다.
    """
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    fresh_floor = now - timedelta(seconds=_RESERVE_STALE_SEC)
    return int(
        session.scalar(
            select(func.count())
            .select_from(EmailSendLogRow)
            .where(
                EmailSendLogRow.sent_at >= start,
                or_(
                    EmailSendLogRow.status.in_(_DELIVERED),
                    and_(
                        EmailSendLogRow.status == "sending",
                        EmailSendLogRow.sent_at >= fresh_floor,
                    ),
                ),
            )
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
    remaining = max(0, cap - _today_used_count(session, now))
    return {
        "recipients": len(recips),
        "enabled": bool(settings.email_send_enabled),
        "daily_cap": cap,
        "remaining_today": remaining,
        "sender": settings.smtp_send_user,
        "sample": [e for _, e in recips[:sample]],
    }


# 예약(sending) 행이 이 시간을 넘기면 좌초로 보고 재예약을 허용한다 — SMTP 도중 프로세스가
# 죽어 'sending' 박제가 상한·재발송 차단을 영원히 점유하는 것 방지.
_RESERVE_STALE_SEC = 600

# PG advisory lock 키(임의 고정 상수) — 예약 트랜잭션 직렬화(카운트→INSERT 레이스 제거).
_SEND_LOCK_KEY = 872_634_121


def _reserve_send(
    settings: Settings,
    *,
    email: str,
    company_id: str,
    subject: str,
    sent_by: str | None,
    cap: int,
    now: datetime,
) -> str:
    """수신자 1명을 발송 전에 원자적으로 선점한다 — 'reserved' | 'dup' | 'capped'.

    격리 트랜잭션에서 (PG 면 advisory xact lock 으로 직렬화 후) 오늘 사용량을 세고,
    주소 PK 행을 status='sending' 으로 확보한다. 더블클릭/동시 캠페인의 양쪽이 같은
    수신자를 미발송으로 판단해 두 통 나가던 레이스(전수리뷰)를 DB 선점으로 차단.
    이미 sent·uncertain(또는 신선한 sending) 행이 있으면 'dup', 오늘 상한 소진이면 'capped'.
    실패(failed)·좌초(sending 이 _RESERVE_STALE_SEC 초과) 행은 재예약해 재시도를 살린다.

    ponytail 잔여 한계(교차리뷰 합의): ①비PG(SQLite)는 advisory lock 이 없어 서로 다른
    이메일 간 상한 레이스가 이론상 가능 — 운영 DB 는 PG 전제. ②재예약에 펜싱 토큰이
    없으나, SMTP 타임아웃(10s)이 stale 창(600s)보다 훨씬 짧아 '진행 중인데 stale 로
    오판'은 예약 시각을 매 건 신선하게 찍는 한 실현 불가(send_campaign 쪽 보장).
    """
    if now.tzinfo is None:  # naive 입력 방어 — UTC 저장 규약으로 재해석.
        now = now.replace(tzinfo=timezone.utc)
    with session_scope(settings) as res:
        if res.get_bind().dialect.name == "postgresql":
            # 예약 직렬화 — count→INSERT 사이 창을 닫는다(다중 워커·다중 요청 공통).
            res.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _SEND_LOCK_KEY})
        if _today_used_count(res, now) >= cap:
            return "capped"
        rid = _send_id(email)
        row = res.get(EmailSendLogRow, rid)
        if row is not None:
            if row.status in _DELIVERED:
                return "dup"
            if row.status == "sending" and row.sent_at is not None:
                # SQLite 는 naive 로 돌려준다(UTC 저장 규약) — aware 로 정규화 후 비교.
                stamp = row.sent_at if row.sent_at.tzinfo else row.sent_at.replace(tzinfo=timezone.utc)
                age = (now - stamp).total_seconds()
                if age < _RESERVE_STALE_SEC:
                    return "dup"  # 다른 캠페인이 지금 보내는 중.
        else:
            row = EmailSendLogRow(id=rid, email=email, company_id=company_id)
            res.add(row)
        row.company_id = company_id
        row.subject = subject[:512]
        row.status = "sending"
        row.error = None
        row.sent_by = sent_by
        row.sent_at = now
        try:
            res.flush()
        except IntegrityError:  # 동시 INSERT 경합(비 PG 경로 방어) — 상대가 선점.
            res.rollback()
            return "dup"
    return "reserved"


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
    injected_now = now  # 테스트 주입용 고정 시각 — 미주입(운영)이면 수신자마다 신선하게 찍는다.
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
            "uncertain": 0,
            "skipped": 0,
            "capped": 0,
        }

    cap = max(0, settings.email_send_daily_cap)
    sent = failed = skipped = uncertain = 0
    capped = 0
    conn: smtplib.SMTP | None = None  # 캠페인 동안 SMTP 연결 1개 재사용(send_one 이 돌려줌).
    try:
        for i, (company_id, email) in enumerate(recips):
            # 예약·확정 시각은 수신자마다 신선하게 — 캠페인 시작 시각을 재사용하면 레이트리밋으로
            # 긴 캠페인의 후반 예약이 '이미 stale' 로 태어나 동시 캠페인이 이중발송한다(교차리뷰 HIGH).
            tick = injected_now or _utcnow()
            # 발송 **전** DB 선점(격리 커밋) — 동시 요청/더블클릭의 상대편이 이 수신자·상한
            # 슬롯을 쓸 수 없게 한다. 'dup'=상대가 선점/기발송(스킵), 'capped'=오늘 상한 소진.
            outcome = _reserve_send(
                settings, email=email, company_id=company_id, subject=subject,
                sent_by=sent_by, cap=cap, now=tick,
            )
            if outcome == "capped":
                capped = len(recips) - i
                break
            if outcome == "dup":
                skipped += 1
                continue
            try:
                # 재사용 연결 생존 확인은 **발송 전 NOOP** 으로만 — 발송 중 끊김을 재시도하면
                # DATA 수락 후 응답 읽기에서 끊긴 경우(같은 SMTPServerDisconnected)에 이중발송.
                # 발송 자체는 절대 재시도하지 않는다(실패=failed 1건, 다음 수신자는 새 연결).
                if conn is not None and not _smtp_alive(conn):
                    _smtp_quit(conn)
                    conn = None
                conn = send_one(
                    settings, to=email, subject=subject, body=body,
                    from_display=from_display, server=conn,
                )
                status, error = "sent", None
                sent += 1
            except SendUncertain as exc:  # 전달됐을 수 있음 — 자동 재시도 금지(이중발송 방지).
                _smtp_quit(conn)
                conn = None
                status, error = "uncertain", str(exc)[:500]
                uncertain += 1
                log.warning("outreach.send_uncertain", email=email, err=str(exc))
            except Exception as exc:  # 한 통 실패가 캠페인 전체를 막지 않게(로그 후 계속).
                _smtp_quit(conn)
                conn = None  # 연결 상태 불명 → 다음 수신자는 새로 접속.
                status, error = "failed", str(exc)[:500]
                failed += 1
                log.info("outreach.send_error", email=email, err=str(exc))
            # 비가역 SMTP 발송 직후 **격리 트랜잭션**으로 결과를 즉시 영속화한다(예약행을
            # sent/failed 로 확정). SMTP 직후 프로세스가 죽어도 예약행(sending)이 남아,
            # 재실행 중복발송은 stale 창(_RESERVE_STALE_SEC) 이후에만 가능하도록 좁힌다.
            with session_scope(settings) as log_session:
                _log_send(log_session, email=email, company_id=company_id, subject=subject,
                          status=status, error=error, sent_by=sent_by, now=injected_now or _utcnow())
            if i < len(recips) - 1 and settings.email_send_min_interval > 0:
                sleep(settings.email_send_min_interval)  # 레이트리밋(계정 차단 방지).
    finally:
        _smtp_quit(conn)
    log.info(
        "outreach.sent", sent=sent, failed=failed, uncertain=uncertain, skipped=skipped, capped=capped
    )
    return {
        "dry_run": False,
        "recipients": len(recips),
        "attempted": sent + failed + uncertain,
        "sent": sent,
        "failed": failed,
        "uncertain": uncertain,  # 결과 불명(재발송 자동 제외 — 운영자 확인 대상).
        "skipped": skipped,  # 동시 캠페인 선점/기발송 스킵(additive 키).
        "capped": capped,  # 일일 상한 초과로 미발송.
    }
