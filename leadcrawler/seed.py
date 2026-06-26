"""로컬 개발용 목(mock) 시드 데이터.

docker-compose 의 PostgreSQL 을 띄우고(`docker compose up -d`) 마이그레이션을 적용한
뒤(`leadcrawler db-upgrade`), 검증 웹앱을 빈 화면 대신 실제 행으로 둘러보기 위한
결정적 더미 리드 5건을 만든다. 네트워크 없이 동작하며, ``save_lead`` 의 멱등 경로를
타므로 여러 번 실행해도 중복이 생기지 않는다(canonical_key 기준).

전 행이 프로젝트 제약을 지킨다:
- 제약 ②: 실존(active + 도메인 생존) 기업만 — ``is_active=True``, ``site_alive=True``.
- 이메일 role: IR 우선 + general(contact/info) 허용, HR·언론 배제(IR/GENERAL 만 사용).
- 이메일 없이 문의폼만 있는 회사 1건 포함(엑셀 J="사이트 내 문의폼" 경로 확인용).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from .dedup import canonical_key
from .models import (
    Company,
    CompanyLead,
    Contact,
    ContactType,
    EmailRole,
    EmailValidation,
    Listed,
    ValidationStatus,
)
from .storage.repository import save_lead

MOCK_SOURCE = "mock"


def _company(name: str, *, country: str, industry: str, listed: Listed, domain: str) -> Company:
    """실존 판정이 선 회사 프로필을 만든다(도메인 기반 canonical_key)."""
    return Company(
        canonical_key=canonical_key(domain=domain),
        name=name,
        country=country,
        industry=industry,
        listed=listed,
        homepage=f"https://www.{domain}/",
        domain=domain,
        is_active=True,
        existence_confidence=0.95,
        site_alive=True,
    )


def _email(value: str, *, role: EmailRole = EmailRole.IR) -> Contact:
    return Contact(type=ContactType.EMAIL, value=value, role=role, confidence=0.9)


def _valid(status: ValidationStatus, *, mx: bool, smtp: bool | None) -> EmailValidation:
    return EmailValidation(status=status, mx=mx, domain_match=True, smtp=smtp)


def build_mock_leads() -> list[CompanyLead]:
    """검증 워크벤치를 둘러볼 결정적 더미 리드 5건을 만든다(다양한 케이스 망라)."""
    leads: list[CompanyLead] = []

    # 1) 단일 IR 이메일 — 완전 검증(valid+mx+smtp). 정상 처리 happy-path.
    e1 = _email("ir@samsungcnt.com")
    leads.append(
        CompanyLead(
            company=_company(
                "삼성물산", country="KR", industry="건설", listed=Listed.LISTED,
                domain="samsungcnt.com",
            ),
            email=e1,
            email_candidates=[e1],
            phone=Contact(type=ContactType.PHONE, value="+82-2-2145-5114"),
            email_validations={e1.value: _valid(ValidationStatus.VALID, mx=True, smtp=True)},
        )
    )

    # 2) 다중 후보(IR + general) — 워크벤치의 후보 선택 UI 확인. 후보별 신호가 다르다.
    e2a = _email("ir@hdec.co.kr")
    e2b = _email("contact@hdec.co.kr", role=EmailRole.GENERAL)
    leads.append(
        CompanyLead(
            company=_company(
                "현대건설", country="KR", industry="건설", listed=Listed.LISTED,
                domain="hdec.co.kr",
            ),
            email=e2a,
            email_candidates=[e2a, e2b],
            phone=Contact(type=ContactType.PHONE, value="+82-2-1577-7755"),
            email_validations={
                e2a.value: _valid(ValidationStatus.VALID, mx=True, smtp=True),
                e2b.value: _valid(ValidationStatus.RISKY, mx=True, smtp=None),
            },
        )
    )

    # 3) general 이메일(info@) — 해외(US) 비상장. catch-all 로 smtp 판정불가(None).
    e3 = _email("info@bechtel.com", role=EmailRole.GENERAL)
    leads.append(
        CompanyLead(
            company=_company(
                "Bechtel", country="US", industry="건설", listed=Listed.UNLISTED,
                domain="bechtel.com",
            ),
            email=e3,
            email_candidates=[e3],
            email_validations={e3.value: _valid(ValidationStatus.UNKNOWN, mx=True, smtp=None)},
        )
    )

    # 4) IR 이메일 — 해외(FR) 상장. mx 만 통과(smtp 미시도).
    e4 = _email("ir.contact@vinci.com")
    leads.append(
        CompanyLead(
            company=_company(
                "VINCI", country="FR", industry="건설", listed=Listed.LISTED,
                domain="vinci.com",
            ),
            email=e4,
            email_candidates=[e4],
            phone=Contact(type=ContactType.PHONE, value="+33-1-47-16-35-00"),
            email_validations={e4.value: _valid(ValidationStatus.RISKY, mx=True, smtp=None)},
        )
    )

    # 5) 이메일 없음 — 문의폼만 보유(엑셀 J="사이트 내 문의폼" 경로). 사람이 워크벤치에서
    #    직접 이메일을 찾거나 폼으로 처리한다(빈 후보로 큐 등록).
    leads.append(
        CompanyLead(
            company=_company(
                "대우건설", country="KR", industry="건설", listed=Listed.LISTED,
                domain="daewooenc.com",
            ),
            form=Contact(
                type=ContactType.FORM, value="https://www.daewooenc.com/customer/inquiry.do"
            ),
        )
    )

    return leads


def seed_mock_leads(session: Session, leads: Sequence[CompanyLead] | None = None) -> int:
    """목 리드를 DB 에 멱등 적재한다(발견원장·회사·연락처·검증 큐). 적재 건수를 반환한다."""
    rows = list(leads) if leads is not None else build_mock_leads()
    for lead in rows:
        save_lead(session, lead, source=MOCK_SOURCE)
    return len(rows)
