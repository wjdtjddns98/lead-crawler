"""이메일 유효성 검증 (공격적·고품질).

단계: 형식 → MX 레코드 → 회사 도메인 일치 → (실 경로) 유료 검증 API/SMTP.
dry_run 에서는 네트워크 없이 형식·도메인 일치만으로 결정적 판정한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..config import Settings, get_settings
from ..dedup import normalize_domain
from ..models import EmailValidation, ValidationStatus


def _format_ok(email: str) -> bool:
    return bool(email) and email.count("@") == 1 and "." in email.split("@", 1)[1]


def _has_mx(domain: str, settings: Settings) -> bool:
    """MX 레코드 존재 여부. dry_run 이면 네트워크 없이 형식 휴리스틱."""
    if settings.dry_run:
        return "." in domain
    import dns.resolver

    try:
        return bool(dns.resolver.resolve(domain, "MX"))
    except Exception:
        return False


class EmailValidator:
    """이메일 deliverability 를 다단계로 검증한다."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

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
        mx = _has_mx(email_domain, self.settings)

        if not mx:
            status = ValidationStatus.INVALID
        elif domain_match or not company_domain:
            status = ValidationStatus.VALID
        else:
            status = ValidationStatus.RISKY

        return EmailValidation(
            status=status,
            mx=mx,
            domain_match=domain_match,
            provider="dry_run" if self.settings.dry_run else "mx",
            checked_at=now,
        )
