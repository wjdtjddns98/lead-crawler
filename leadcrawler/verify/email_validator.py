"""이메일 유효성 검증 (공격적·고품질).

단계: 형식 → MX 레코드 → 회사 도메인 일치 → (opt-in) SMTP RCPT 메일박스 프로브.
dry_run 에서는 네트워크 없이 형식·도메인 일치만으로 결정적 판정한다(SMTP 미시도).

SMTP 프로브(``email_smtp_check`` 켤 때만, 라이브):
- MX 호스트에 ``RCPT TO`` 로 수신 가능 여부를 본다(250=수신, 550=없음).
- catch-all(아무 주소나 250) 서버는 판정 불가로 처리(과신 방지).
프로버는 주입 가능(테스트는 네트워크 없이 가짜 프로버로 분기 검증).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from ..config import Settings, get_settings
from ..dedup import normalize_domain
from ..logging import get_logger
from ..models import EmailValidation, ValidationStatus

log = get_logger("verify.email")

# SMTP 프로브 판정값.
SMTP_DELIVERABLE = "deliverable"  # 수신 확정(250, catch-all 아님)
SMTP_UNDELIVERABLE = "undeliverable"  # 메일박스 없음(550 등)
SMTP_UNKNOWN = "unknown"  # 미시도·catch-all·타임아웃 등 판정 불가
# catch-all 탐지용 비존재 가능성 높은 로컬파트(이 주소도 250 이면 catch-all).
_CATCHALL_PROBE_LOCAL = "no-such-mailbox-leadcrawler-probe"


def _format_ok(email: str) -> bool:
    return bool(email) and email.count("@") == 1 and "." in email.split("@", 1)[1]


def _resolve_mx(domain: str, settings: Settings) -> tuple[bool, list[str]]:
    """MX 존재 여부 + 우선순위 정렬된 MX 호스트 목록을 반환한다.

    dry_run 이면 네트워크 없이 형식 휴리스틱(호스트 목록은 빈 채로).
    """
    if settings.dry_run:
        return ("." in domain, [])
    import dns.resolver

    try:
        answer = dns.resolver.resolve(domain, "MX")
        hosts = [
            str(r.exchange).rstrip(".")
            for r in sorted(answer, key=lambda r: r.preference)
            if str(r.exchange).strip(".")
        ]
        return (bool(hosts), hosts)
    except Exception:
        return (False, [])


class SupportsSmtpProbe(Protocol):
    """SMTP 메일박스 프로버 인터페이스(테스트 더블이 구현)."""

    def probe(self, email: str, mx_hosts: list[str]) -> str:
        """``SMTP_DELIVERABLE`` / ``SMTP_UNDELIVERABLE`` / ``SMTP_UNKNOWN`` 중 하나."""
        ...


class SmtpProber:
    """smtplib 기반 실 SMTP RCPT 프로버(catch-all 탐지 포함)."""

    def __init__(self, mail_from: str, *, timeout: float = 10.0) -> None:
        self._from = mail_from
        self._timeout = timeout

    def probe(self, email: str, mx_hosts: list[str]) -> str:
        import smtplib

        domain = email.split("@", 1)[1]
        catchall_addr = f"{_CATCHALL_PROBE_LOCAL}@{domain}"
        for host in mx_hosts[:2]:  # 상위 2개 MX 만 시도(비용·시간 제한).
            try:
                with smtplib.SMTP(host, 25, timeout=self._timeout) as smtp:
                    smtp.ehlo_or_helo_if_needed()
                    smtp.mail(self._from)
                    real_code, _ = smtp.rcpt(email)
                    if real_code in (550, 551, 553):
                        return SMTP_UNDELIVERABLE  # 메일박스 없음(하드 바운스).
                    if real_code != 250:
                        continue  # 521(호스트 거부)·4xx 그레이리스팅 등 → 다음 호스트.
                    # 250: catch-all 인지 비존재 주소로 재확인.
                    probe_code, _ = smtp.rcpt(catchall_addr)
                    return SMTP_UNKNOWN if probe_code == 250 else SMTP_DELIVERABLE
            except Exception as exc:  # 연결 거부·타임아웃 → 다음 호스트.
                log.info("smtp.probe.error", host=host, err=str(exc))
                continue
        return SMTP_UNKNOWN


class EmailValidator:
    """이메일 deliverability 를 다단계로 검증한다."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        smtp_prober: SupportsSmtpProbe | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._smtp_prober = smtp_prober

    def _prober(self) -> SupportsSmtpProbe:
        if self._smtp_prober is None:
            self._smtp_prober = SmtpProber(
                self.settings.email_smtp_from, timeout=self.settings.smtp_timeout
            )
        return self._smtp_prober

    def _placeholder_from(self) -> bool:
        """MAIL FROM 이 비었거나 예약(example.com 등)·로컬 도메인이면 라이브 부적합."""
        frm = (self.settings.email_smtp_from or "").strip().lower()
        return (
            not frm
            or "@" not in frm
            or frm.endswith(("example.com", "example.org", "example.net", ".local"))
        )

    def validate(self, email: str, company_domain: str | None = None) -> EmailValidation:
        """이메일 1건을 검증해 :class:`EmailValidation` 을 반환한다."""
        now = datetime.now(timezone.utc)
        if not _format_ok(email):
            return EmailValidation(status=ValidationStatus.INVALID, checked_at=now)

        email_domain = email.split("@", 1)[1].lower()
        domain_match = (
            normalize_domain(company_domain) == normalize_domain(email_domain)
            if company_domain
            else False
        )
        mx, mx_hosts = _resolve_mx(email_domain, self.settings)

        # 1차: MX + 도메인 일치 기반 판정.
        if not mx:
            status = ValidationStatus.INVALID
        elif domain_match or not company_domain:
            status = ValidationStatus.VALID
        else:
            status = ValidationStatus.RISKY

        # 2차(opt-in 라이브): SMTP RCPT 프로브로 보정.
        smtp_result = SMTP_UNKNOWN
        provider = "dry_run" if self.settings.dry_run else "mx"
        if mx and not self.settings.dry_run and self.settings.email_smtp_check:
            if self._placeholder_from():
                # 예약/빈 MAIL FROM 으로 라이브 프로브 시 차단·오판 위험 → 스킵(MX 판정 유지).
                log.info("smtp.skip.placeholder_from", mail_from=self.settings.email_smtp_from)
            else:
                smtp_result = self._prober().probe(email, mx_hosts)
                if smtp_result != SMTP_UNKNOWN:
                    provider = "smtp"  # SMTP 가 실제 판정에 기여한 경우만 출처 표기.
                if smtp_result == SMTP_UNDELIVERABLE:
                    status = ValidationStatus.INVALID  # 메일박스 없음 → 무효 확정.
                elif smtp_result == SMTP_DELIVERABLE and status is ValidationStatus.RISKY:
                    status = ValidationStatus.VALID  # 수신 확정 → RISKY 승격.

        smtp_flag = {SMTP_DELIVERABLE: True, SMTP_UNDELIVERABLE: False}.get(smtp_result)
        return EmailValidation(
            status=status,
            mx=mx,
            domain_match=domain_match,
            smtp=smtp_flag,
            provider=provider,
            checked_at=now,
        )
