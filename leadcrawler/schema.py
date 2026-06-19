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
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
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
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
