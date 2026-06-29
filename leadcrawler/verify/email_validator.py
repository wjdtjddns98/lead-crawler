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
from ..cost_ledger import SupportsCostLedger
from ..dedup import normalize_domain
from ..logging import get_logger
from ..models import EmailValidation, ValidationStatus
from .deliverability import (
    DELIVERABLE as DELIV_OK,
)
from .deliverability import (
    UNDELIVERABLE as DELIV_BAD,
)
from .deliverability import (
    SupportsDeliverability,
    build_deliverability_checker,
)

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
        deliverability_checker: SupportsDeliverability | None = None,
        cost_ledger: SupportsCostLedger | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._smtp_prober = smtp_prober
        self._deliverability = deliverability_checker
        # 주입되면 더는 빌드하지 않는다(테스트 더블·명시 None 모두 존중).
        self._deliverability_built = deliverability_checker is not None
        self._cost_ledger = cost_ledger
        self._deliv_fetcher: object | None = None  # 지연 생성 Fetcher(close 대상).
        # 도메인 MX 조회 메모이즈 — 같은 회사 도메인 후보 N개의 DNS MX 왕복을 1회로 축약.
        self._mx_cache: dict[str, tuple[bool, list[str]]] = {}

    def _mx(self, email_domain: str) -> tuple[bool, list[str]]:
        """도메인 MX 조회를 인스턴스 범위로 메모이즈한다(같은 도메인 후보 N회→1회).

        같은 회사 도메인의 이메일 후보가 여럿이면 동일 MX 를 반복 조회하던 DNS 왕복을
        제거한다(negative 결과도 캐시해 죽은 도메인 재조회 회피). MX 는 런 내 도메인안정
        이고 인스턴스가 워커 스레드 전용(run.py 워커별 독립 EmailValidator)이라 스레드안전.
        반환 호스트 리스트는 호출부에서 슬라이스만 하고 변형하지 않아 공유 안전.
        """
        cached = self._mx_cache.get(email_domain)
        if cached is None:
            cached = _resolve_mx(email_domain, self.settings)
            self._mx_cache[email_domain] = cached
        return cached

    def _prober(self) -> SupportsSmtpProbe:
        if self._smtp_prober is None:
            self._smtp_prober = SmtpProber(
                self.settings.email_smtp_from, timeout=self.settings.smtp_timeout
            )
        return self._smtp_prober

    def _deliv_checker(self) -> SupportsDeliverability | None:
        """키 있는 딜리버러빌리티 제공자(없으면 None). 라이브에서 1회 지연 생성."""
        if not self._deliverability_built:
            from ..sources.http import Fetcher

            fetcher = Fetcher(
                user_agent=self.settings.discovery_user_agent,
                min_interval=self.settings.http_request_delay,
                timeout=self.settings.http_timeout,
            )
            self._deliv_fetcher = fetcher
            self._deliverability = build_deliverability_checker(self.settings, fetcher=fetcher)
            self._deliverability_built = True
        return self._deliverability

    def close(self) -> None:
        """지연 생성한 딜리버러빌리티 Fetcher(httpx)를 정리한다(병렬 워커 누수 방지)."""
        close = getattr(self._deliv_fetcher, "close", None)
        if callable(close):
            close()

    def _budget_blocked(self) -> bool:
        """예산 가드 — 원장이 있고 enforce 가 켜졌고 월 누계가 예산 이상이면 차단."""
        led = self._cost_ledger
        if led is None or not self.settings.cost_budget_enforce:
            return False
        if led.is_over_budget():
            log.info("cost.budget.blocked", budget_krw=self.settings.monthly_budget_krw)
            return True
        return False

    def _record_cost(self, provider: str, units: int = 1) -> None:
        """유료 호출 1건을 원장에 적재(원장 없으면 no-op)."""
        if self._cost_ledger is not None:
            self._cost_ledger.record(provider, units)

    def _placeholder_from(self) -> bool:
        """MAIL FROM 이 비었거나 예약(example.com 등)·로컬 도메인이면 라이브 부적합."""
        frm = (self.settings.email_smtp_from or "").strip().lower()
        return (
            not frm
            or "@" not in frm
            or frm.endswith(("example.com", "example.org", "example.net", ".local"))
        )

    def validate(
        self, email: str, company_domain: str | None = None, *, deep: bool = True
    ) -> EmailValidation:
        """이메일 1건을 검증해 :class:`EmailValidation` 을 반환한다.

        ``deep`` 이 False 면 형식·MX·도메인일치까지만 하고 SMTP RCPT·딜리버러빌리티(유료)는
        건너뛴다 — 선택되지 않은 후보의 핸드셰이크·과금 곱셈을 줄이기 위한 경량 경로다.
        """
        now = datetime.now(timezone.utc)
        if not _format_ok(email):
            return EmailValidation(status=ValidationStatus.INVALID, checked_at=now)

        email_domain = email.split("@", 1)[1].lower()
        domain_match = (
            normalize_domain(company_domain) == normalize_domain(email_domain)
            if company_domain
            else False
        )
        mx, mx_hosts = self._mx(email_domain)

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
        if deep and mx and not self.settings.dry_run and self.settings.email_smtp_check:
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

        # 3차(opt-in 라이브·유료): 딜리버러빌리티 API 로 최종 보정. 이미 INVALID 면
        # 제외 확정이라 과금 호출을 아낀다(VALID/RISKY 만 질의).
        if (
            deep
            and mx
            and not self.settings.dry_run
            and self.settings.email_deliverability_check
            and status is not ValidationStatus.INVALID
            and not self._budget_blocked()
        ):
            checker = self._deliv_checker()
            if checker is not None:
                verdict = checker.check(email)
                self._record_cost(checker.name)  # 유료 딜리버러빌리티 호출 1건.
                if verdict == DELIV_BAD:
                    status = ValidationStatus.INVALID  # 제3자 DB 수신불가 → 무효 확정.
                    provider = checker.name
                elif verdict == DELIV_OK:
                    if status is ValidationStatus.RISKY:
                        status = ValidationStatus.VALID  # 수신 확정 → RISKY 승격.
                    provider = checker.name  # 권위있는 확정 → 출처 표기.

        smtp_flag = {SMTP_DELIVERABLE: True, SMTP_UNDELIVERABLE: False}.get(smtp_result)
        return EmailValidation(
            status=status,
            mx=mx,
            domain_match=domain_match,
            smtp=smtp_flag,
            provider=provider,
            checked_at=now,
        )
