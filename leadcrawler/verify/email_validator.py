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

# 공유 플랫폼·디렉토리 도메인(검색 blocklist 재사용) — 소속(domain_match) 판정에서 제외.
from ..sources.search import _BLOCKLIST as _SHARED_PLATFORM_DOMAINS
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


def _resolve_mx(domain: str, settings: Settings) -> tuple[bool | None, list[str]]:
    """MX 존재 여부 + 우선순위 정렬된 MX 호스트 목록을 반환한다.

    첫 값은 **3상태**다: ``True``=MX 있음, ``False``=**확정 부재**(도메인 없음/MX 레코드
    없음), ``None``=**조회 실패**(타임아웃·SERVFAIL 등 일시 장애 가능 — 판정 불가).

    구분하는 이유: 예전엔 모든 예외를 ``False`` 로 뭉개 일시적 DNS 장애가 곧 INVALID 가
    됐고, 이메일 상한(emailrules.cap_emails)이 INVALID 를 제외하면서 **일시 장애 한 번에
    그 회사 이메일이 통째로 삭제**될 수 있었다(2026-07-29 리뷰 지적). 확정 부재만 무효로
    본다.

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
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return (False, [])  # 확정: 도메인 부재 또는 MX 레코드 없음.
    except Exception:
        # 타임아웃·SERVFAIL·리졸버 오류 등 — 일시 장애일 수 있어 무효로 확정하지 않는다.
        return (None, [])


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

    def _mx(self, email_domain: str) -> tuple[bool | None, list[str]]:
        """도메인 MX 조회를 인스턴스 범위로 메모이즈한다(같은 도메인 후보 N회→1회).

        같은 회사 도메인의 이메일 후보가 여럿이면 동일 MX 를 반복 조회하던 DNS 왕복을
        제거한다(negative 결과도 캐시해 죽은 도메인 재조회 회피). MX 는 런 내 도메인안정
        이고 인스턴스가 워커 스레드 전용(run.py 워커별 독립 EmailValidator)이라 스레드안전.
        반환 호스트 리스트는 호출부에서 슬라이스만 하고 변형하지 않아 공유 안전.
        """
        cached = self._mx_cache.get(email_domain)
        if cached is None:
            cached = _resolve_mx(email_domain, self.settings)
            # 조회 실패(None)는 캐시하지 않는다 — 일시 장애를 런 내내 박제하면 그 도메인의
            # 뒤 후보까지 전부 판정불가로 끌고 간다. 확정 결과(True/False)만 메모이즈.
            if cached[0] is not None:
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
        norm_email = normalize_domain(email_domain)
        # 소속 일치 = 정규화 도메인 동일 + 그 도메인이 공유 플랫폼(_BLOCKLIST)이 아닐 것.
        # normalize_domain 은 tistory/blogspot 류 블로그 루트를 등록도메인으로 뭉개므로
        # acme.tistory.com(회사) ↔ help@tistory.com(플랫폼 공용)이 '일치'로 오판된다 —
        # 플랫폼 루트는 누구의 소속 증명도 될 수 없다(2026-08-14 교차리뷰 MED).
        domain_match = bool(
            company_domain
            and norm_email
            and normalize_domain(company_domain) == norm_email
            and norm_email not in _SHARED_PLATFORM_DOMAINS
        )
        mx, mx_hosts = self._mx(email_domain)

        # 1차: MX + 도메인 일치 기반 판정.
        if mx is None:
            # MX 조회 실패(일시 장애 가능) — 무효로 확정하지 않고 '주의'로 보존한다.
            # cap_emails 가 INVALID 만 버리므로, 이 이메일은 살아남아 재검증 기회를 얻는다.
            status = ValidationStatus.RISKY
        elif not mx:
            status = ValidationStatus.INVALID
        elif domain_match:
            status = ValidationStatus.VALID
        else:
            # 도메인 불일치·회사 도메인 미상 = **소속 불명** → '주의'(사람 확인 대상).
            # VALID('정상')는 "수신 가능 + 회사 소속(도메인 일치)" 둘 다 확인된 경우만 —
            # 호스팅사·플랫폼·제작사 등 제3자 이메일이 수신 가능하다는 이유로 '정상'을
            # 달고 나가던 오염 경로 차단(2026-08-14 검증팀 피드백).
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
                # 수신 확정(DELIVERABLE)이어도 승격하지 않는다 — 수신 가능은 메일박스
                # 존재 증명일 뿐 회사 소속 증명이 아니다(제3자 이메일 '정상' 오염 방지).

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
                    # 수신 가능 확정이어도 승격 없음(위 SMTP 와 동일 사유) — 출처만 표기.
                    provider = checker.name

        smtp_flag = {SMTP_DELIVERABLE: True, SMTP_UNDELIVERABLE: False}.get(smtp_result)
        return EmailValidation(
            status=status,
            mx=bool(mx),  # 조회 실패(None)는 "MX 확인 못 함" → 표시상 False(상태는 RISKY).
            domain_match=domain_match,
            smtp=smtp_flag,
            provider=provider,
            checked_at=now,
        )
