"""파이프라인 본체 — 발견부터 CompanyLead 까지.

제약 ①(중복) : ``seen`` canonical_key 집합으로 이미 본 기업을 스킵.
제약 ②(실존) : ExistenceVerifier 로 죽은 기업을 거른다(검증 큐 대상).
dry_run 에서는 모든 단계가 네트워크 없이 결정적으로 동작한다.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..config import Settings, get_settings
from ..emailrules import select_best_email
from ..logging import get_logger
from ..models import (
    Company,
    CompanyLead,
    Contact,
    ContactType,
    EmailRole,
    EmailValidation,
    ExtractMethod,
    Listed,
)
from ..sources.base import DiscoveredCompany, DummySource, Segment
from ..verify.email_validator import EmailValidator
from ..verify.existence import ExistenceVerifier

log = get_logger("pipeline")


def dry_run_enrich(dc: DiscoveredCompany) -> list[Contact]:
    """dry_run 보강 — 도메인 기반 결정적 연락처(이메일·전화·폼)."""
    domain = dc.domain or "example.com"
    return [
        Contact(
            type=ContactType.EMAIL,
            value=f"ir@{domain}",
            role=EmailRole.IR,
            extract_method=ExtractMethod.STATIC,
            source_url=f"https://{domain}/investor",
            confidence=0.9,
        ),
        Contact(
            type=ContactType.PHONE,
            value="+82-2-0000-0000",
            extract_method=ExtractMethod.STATIC,
            confidence=0.6,
        ),
        Contact(
            type=ContactType.FORM,
            value=f"https://{domain}/contact",
            extract_method=ExtractMethod.STATIC,
            confidence=0.8,
        ),
    ]


def run_pipeline(
    segments: Iterable[Segment],
    *,
    seen: set[str] | None = None,
    settings: Settings | None = None,
) -> list[CompanyLead]:
    """세그먼트들을 처리해 검증된 :class:`CompanyLead` 목록을 반환한다."""
    settings = settings or get_settings()
    seen = seen if seen is not None else set()
    source = DummySource()
    existence = ExistenceVerifier(settings)
    email_validator = EmailValidator(settings)

    leads: list[CompanyLead] = []
    for segment in segments:
        for dc in source.discover(segment):
            if dc.canonical_key in seen:
                log.info("dedup.skip", key=dc.canonical_key)
                continue
            seen.add(dc.canonical_key)

            contacts = dry_run_enrich(dc) if settings.dry_run else []
            email = select_best_email(contacts)
            phone = next((c for c in contacts if c.type is ContactType.PHONE), None)
            form = next((c for c in contacts if c.type is ContactType.FORM), None)

            ex = existence.verify(dc.domain)
            company = Company(
                canonical_key=dc.canonical_key,
                name=dc.name,
                country=dc.country,
                industry=dc.industry,
                listed=Listed.UNKNOWN,
                homepage=f"https://{dc.domain}" if dc.domain else None,
                domain=dc.domain,
                segment=dc.segment,
                is_active=ex.is_active,
                existence_confidence=ex.confidence,
                site_alive=ex.site_alive,
            )
            validation = (
                email_validator.validate(email.value, dc.domain)
                if email
                else EmailValidation()
            )
            leads.append(
                CompanyLead(
                    company=company, email=email, phone=phone, form=form,
                    email_validation=validation,
                )
            )
    return leads
