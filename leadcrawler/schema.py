"""SQLAlchemy 2.0 테이블 정의 (PostgreSQL 운영, 테스트는 SQLite).

ERD 문서와 1:1 대응. PG 전용 타입을 피해 SQLite 로도 ``create_all`` 가능하게 한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """선언적 베이스."""


class DiscoveredCompanyRow(Base):
    __tablename__ = "discovered_company"

    canonical_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(512))
    country: Mapped[str] = mapped_column(String(8), default="")
    industry: Mapped[str] = mapped_column(String(128), default="")
    listed: Mapped[str] = mapped_column(String(16), default="unknown")
    registry: Mapped[str | None] = mapped_column(String(32), nullable=True)
    registry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    segment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_crawled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CompanyRow(Base):
    __tablename__ = "company"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    canonical_key: Mapped[str] = mapped_column(
        ForeignKey("discovered_company.canonical_key")
    )
    name: Mapped[str] = mapped_column(String(512))
    country: Mapped[str] = mapped_column(String(8), default="")
    industry: Mapped[str] = mapped_column(String(128), default="")
    homepage: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    existence_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    site_alive: Mapped[bool] = mapped_column(Boolean, default=False)


class ContactRow(Base):
    __tablename__ = "contact"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("company.id"))
    type: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="unknown")
    extract_method: Mapped[str] = mapped_column(String(16), default="static")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)


class EmailValidationRow(Base):
    __tablename__ = "email_validation"

    contact_id: Mapped[str] = mapped_column(ForeignKey("contact.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    mx: Mapped[bool] = mapped_column(Boolean, default=False)
    domain_match: Mapped[bool] = mapped_column(Boolean, default=False)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ReviewQueueRow(Base):
    __tablename__ = "review_queue"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("company.id"))
    field: Mapped[str] = mapped_column(String(32))
    candidates: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    assignee: Mapped[str | None] = mapped_column(String(64), nullable=True)
