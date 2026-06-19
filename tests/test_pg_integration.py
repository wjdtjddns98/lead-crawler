"""PostgreSQL 전용 통합 테스트 — SQLite 가 구조적으로 못 잡는 결함 검증.

기본 스킵. CI 의 Postgres 서비스에서 ``LEADCRAWLER_PG_TEST=1`` +
``LEADCRAWLER_DATABASE_URL=postgresql+psycopg://...`` 로만 실행된다.

검증 대상(PG 에서만 발현):
- save_lead 재저장 시 FK(email_validation→contact) 무결성(IMMEDIATE FK).
- canonical_key/name 길이 초과가 절단되어 insert 가 성공(varchar 한계).
- DateTime(timezone=True) 라운드트립이 tz-aware(timestamptz).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from leadcrawler.config import Settings
from leadcrawler.dedup import canonical_key
from leadcrawler.models import (
    Company,
    CompanyLead,
    Contact,
    ContactType,
    EmailRole,
    EmailValidation,
    ValidationStatus,
)
from leadcrawler.schema import Base, CompanyRow, DiscoveredCompanyRow, EmailValidationRow
from leadcrawler.storage.db import get_engine, session_scope
from leadcrawler.storage.repository import load_seen_keys, save_lead

pytestmark = pytest.mark.skipif(
    not os.environ.get("LEADCRAWLER_PG_TEST"),
    reason="PG 통합테스트는 LEADCRAWLER_PG_TEST=1 + 실 PostgreSQL 에서만 실행",
)


@pytest.fixture
def pg_settings() -> Settings:
    url = os.environ["LEADCRAWLER_DATABASE_URL"]
    settings = Settings(database_url=url, dry_run=True)
    engine = get_engine(settings)
    # 깨끗한 스키마로 시작(반복 실행 안전).
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return settings


def _lead(domain: str = "x.com", *, name: str = "테스트기업") -> CompanyLead:
    company = Company(
        canonical_key=canonical_key(domain=domain) if domain else "",
        name=name,
        country="KR",
        domain=domain or None,
        is_active=True,
    )
    email = Contact(type=ContactType.EMAIL, value=f"ir@{domain}", role=EmailRole.IR)
    return CompanyLead(
        company=company,
        email=email,
        email_validation=EmailValidation(status=ValidationStatus.VALID, mx=True),
    )


def test_pg_resave_keeps_fk_integrity(pg_settings: Settings) -> None:
    with session_scope(pg_settings) as s:
        save_lead(s, _lead("x.com"))
    # 재저장이 PG 의 IMMEDIATE FK 하에서 깨지지 않아야 한다(과거 CRITICAL).
    with session_scope(pg_settings) as s:
        save_lead(s, _lead("x.com"))
    with session_scope(pg_settings) as s:
        assert len(s.scalars(select(CompanyRow)).all()) == 1
        assert len(s.scalars(select(EmailValidationRow)).all()) == 1


def test_pg_long_name_and_key_are_clipped(pg_settings: Settings) -> None:
    long_name = "가나다라" * 90  # 360자 — name(512) 안, key 는 255 초과분 축약
    key = canonical_key(name=long_name, country="KR")
    assert len(key) <= 255
    company = Company(canonical_key=key, name=long_name, country="KR", is_active=True)
    with session_scope(pg_settings) as s:
        save_lead(s, CompanyLead(company=company))
    with session_scope(pg_settings) as s:
        assert key in load_seen_keys(s)


def test_pg_datetime_is_timezone_aware(pg_settings: Settings) -> None:
    with session_scope(pg_settings) as s:
        save_lead(s, _lead("x.com"))
    with session_scope(pg_settings) as s:
        row = s.scalars(select(DiscoveredCompanyRow)).first()
        assert row is not None
        assert row.first_seen.tzinfo is not None
