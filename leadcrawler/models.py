"""도메인 모델 (Pydantic v2).

발견(Discovery) → 보강(Enrich) → 검증(Verify) 파이프라인이 주고받는 값과,
최종 산출 엑셀 서식 한 행(:class:`CompanyLead`)을 정의한다. 주석·docstring 은
한국어, 시각은 timezone-aware UTC 를 사용한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def _new_id() -> str:
    return uuid4().hex[:12]


def _utcnow() -> datetime:
    """timezone-aware UTC now."""
    return datetime.now(timezone.utc)


class Listed(str, Enum):
    """상장 여부."""

    LISTED = "listed"
    UNLISTED = "unlisted"
    UNKNOWN = "unknown"


class ContactType(str, Enum):
    """연락처 종류."""

    EMAIL = "email"
    PHONE = "phone"
    FORM = "form"
    ADDRESS = "address"


class EmailRole(str, Enum):
    """이메일 성격 분류 — HR/언론은 배제 대상."""

    IR = "ir"  # 최우선
    GENERAL = "general"  # contact@, info@, common — 허용
    HR = "hr"  # 채용/인사 — 배제
    PRESS = "press"  # 언론/홍보 — 배제
    PERSONAL = "personal"  # 개인 — 배제
    UNKNOWN = "unknown"


# 엑셀에 채택 가능한(=발송 대상으로 쓸 수 있는) 이메일 role.
ACCEPTED_EMAIL_ROLES: frozenset[EmailRole] = frozenset({EmailRole.IR, EmailRole.GENERAL})


class ExtractMethod(str, Enum):
    """연락처를 어떻게 뽑았는지(출처 신뢰도 가중에 사용)."""

    STATIC = "static"
    HEADLESS = "headless"
    OCR_VISION = "ocr_vision"
    API = "api"
    EXISTING_IMPORT = "existing_import"
    MANUAL = "manual"  # 검증 워크벤치에서 사람이 직접 입력/수정


class ValidationStatus(str, Enum):
    """이메일 유효성 검증 결과."""

    VALID = "valid"
    RISKY = "risky"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class Contact(BaseModel):
    """회사의 단일 연락처(이메일/전화/문의폼/주소)."""

    id: str = Field(default_factory=_new_id)
    type: ContactType
    value: str
    role: EmailRole = EmailRole.UNKNOWN
    extract_method: ExtractMethod = ExtractMethod.STATIC
    source_url: str | None = None
    confidence: float = 0.0
    created_at: datetime = Field(default_factory=_utcnow)


class EmailValidation(BaseModel):
    """이메일 deliverability 검증 결과."""

    status: ValidationStatus = ValidationStatus.UNKNOWN
    mx: bool = False
    domain_match: bool = False
    # SMTP RCPT 프로브 결과: True=메일박스 수신확정, False=없음(550),
    # None=미시도/판정불가(catch-all·타임아웃 등).
    smtp: bool | None = None
    provider: str | None = None
    checked_at: datetime | None = None


class CostEvent(BaseModel):
    """유료 외부 호출 1건의 과금 이벤트 — cost_ledger 가 기록·집계한다."""

    provider: str  # 호출부 식별자(hunter/apollo/zerobounce/neverbounce/vision)
    units: int = 1  # 과금 단위 수(예: Vision 이미지 1장=1)
    unit_cost_krw: int = 0  # 단위당 추정 단가(원)
    cost_krw: int = 0  # units * unit_cost_krw
    occurred_at: datetime = Field(default_factory=_utcnow)
    month_key: str = ""  # 집계 키 YYYY-MM(occurred_at 기준)


class Company(BaseModel):
    """검증 파이프라인이 다루는 회사 프로필."""

    id: str = Field(default_factory=_new_id)
    canonical_key: str
    name: str
    country: str = ""
    industry: str = ""
    listed: Listed = Listed.UNKNOWN
    homepage: str | None = None
    domain: str | None = None
    registry: str | None = None
    registry_id: str | None = None
    segment: str | None = None
    is_active: bool = False
    existence_confidence: float = 0.0
    site_alive: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


class CompanyLead(BaseModel):
    """회사 1건 + 채택된 연락처 — 엑셀 한 행에 대응하는 집계 모델."""

    company: Company
    email: Contact | None = None  # 채택된 대표(기본 선택) 이메일 — best-first 선두
    # 채택된 전체 이메일 후보(best-first). 사람이 검증 웹앱에서 최종 1건을 고른다.
    email_candidates: list[Contact] = Field(default_factory=list)
    phone: Contact | None = None
    form: Contact | None = None
    email_validation: EmailValidation = Field(default_factory=EmailValidation)
    # 후보 주소값 → 검증결과. 후보별 신호를 검증 UI 에 보이기 위해 보관.
    email_validations: dict[str, EmailValidation] = Field(default_factory=dict)
