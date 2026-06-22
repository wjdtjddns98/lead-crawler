"""SQLAlchemy 2.0 테이블 정의 (PostgreSQL 운영, 테스트는 SQLite).

ERD 문서와 1:1 대응. PG 전용 타입을 피해 SQLite 로도 ``create_all`` 가능하게 한다.
운영(24/7 병렬·재크롤)을 고려해:
- 자식 FK 는 ``ON DELETE CASCADE`` (부모 삭제 시 고아행 방지),
- 조회 경로(FK 컬럼·논리키)에 인덱스,
- 회사 논리키 ``canonical_key`` 는 ``UNIQUE`` 로 중복 회사 행을 DB 레벨에서 차단,
- NOT NULL + 기본값 컬럼엔 ``server_default`` (ORM 우회 raw insert 안전).

빈 문자열 컨벤션: country/industry/source 등은 "미상"을 ``''`` 로 표기한다(NULL 아님).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """선언적 베이스."""


class DiscoveredCompanyRow(Base):
    __tablename__ = "discovered_company"

    canonical_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(512))
    country: Mapped[str] = mapped_column(String(8), default="", server_default=text("''"))
    industry: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))
    listed: Mapped[str] = mapped_column(
        String(16), default="unknown", server_default=text("'unknown'")
    )
    registry: Mapped[str | None] = mapped_column(String(32), nullable=True)
    registry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    segment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    last_crawled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CompanyRow(Base):
    __tablename__ = "company"
    __table_args__ = (UniqueConstraint("canonical_key", name="uq_company_canonical_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    canonical_key: Mapped[str] = mapped_column(
        ForeignKey("discovered_company.canonical_key"), index=True
    )
    name: Mapped[str] = mapped_column(String(512))
    country: Mapped[str] = mapped_column(String(8), default="", server_default=text("''"))
    industry: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))
    homepage: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    existence_confidence: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0")
    )
    site_alive: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class ContactRow(Base):
    __tablename__ = "contact"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="unknown", server_default=text("'unknown'"))
    extract_method: Mapped[str] = mapped_column(
        String(16), default="static", server_default=text("'static'")
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))


class EmailValidationRow(Base):
    __tablename__ = "email_validation"

    contact_id: Mapped[str] = mapped_column(
        ForeignKey("contact.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), default="unknown", server_default=text("'unknown'"))
    mx: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    domain_match: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # SMTP RCPT 프로브 결과(nullable): True=수신확정, False=없음, NULL=미시도/판정불가.
    smtp: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CostLedgerRow(Base):
    """유료 외부 호출 1건의 과금 기록 — 월 예산(monthly_budget_krw) 추적용.

    ``month_key``(YYYY-MM)에 인덱스를 둬 월 누계 집계를 빠르게 한다. 실제 호출이
    일어난 건만 적재한다(dry_run·무료 경로는 행 없음).
    """

    __tablename__ = "cost_ledger"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    units: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    unit_cost_krw: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cost_krw: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    month_key: Mapped[str] = mapped_column(String(7), index=True)


class ReviewQueueRow(Base):
    __tablename__ = "review_queue"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    field: Mapped[str] = mapped_column(String(32))
    candidates: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default=text("'pending'"))
    assignee: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 사람이 고른 최종 이메일 후보(candidates 중 1건). 미선택이면 NULL(=기본 대표 사용).
    selected: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # 선택을 사람이 명시했는지. False(자동 기본값)면 재크롤마다 best 로 갱신, True 면 보존.
    selected_by_human: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )


class UserRow(Base):
    """검증 웹앱 직원 계정 — 로그인·assignee 식별. 비밀번호는 scrypt 해시만 저장."""

    __tablename__ = "app_user"  # 'user' 는 PG 예약어라 회피.

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class AuthSessionRow(Base):
    """로그인 세션 — 불투명 토큰의 sha256 만 저장(평문 미보관)."""

    __tablename__ = "auth_session"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
