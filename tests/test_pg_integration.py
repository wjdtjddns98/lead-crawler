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

from datetime import datetime, timezone

from leadcrawler.config import Settings
from leadcrawler.dedup import canonical_key
from leadcrawler.models import (
    Company,
    CompanyLead,
    Contact,
    ContactType,
    EmailRole,
    EmailValidation,
    Listed,
    ValidationStatus,
)
from leadcrawler.schema import Base, CompanyRow, DiscoveredCompanyRow, EmailValidationRow, UserRow
from leadcrawler.security import create_user
from leadcrawler.storage.db import get_engine, get_sessionmaker, session_scope
from leadcrawler.storage.repository import load_seen_keys, save_lead
from leadcrawler.storage.review import claim_work

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


# ── Filtered Claim — PG 동시성(SKIP LOCKED + 조인 잠금 회귀) ──────────────────

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _seed_filtered(pg_settings: Settings) -> tuple[str, str]:
    """KR 4 + US/listed 6 회사 + 두 직원 시드 → (alice_id, bob_id)."""
    rows = [(f"kr{i}.com", "KR", "건설", Listed.UNKNOWN) for i in range(4)]
    rows += [(f"us{i}.com", "US", "Finance", Listed.LISTED) for i in range(6)]
    with session_scope(pg_settings) as s:
        for dom, country, industry, listed in rows:
            lead = CompanyLead(
                company=Company(
                    canonical_key=f"dom:{dom}", name=dom, country=country, industry=industry,
                    domain=dom, is_active=True, site_alive=True, listed=listed,
                ),
                email=Contact(type=ContactType.EMAIL, value=f"ir@{dom}", role=EmailRole.IR),
                email_validation=EmailValidation(status=ValidationStatus.VALID, mx=True),
            )
            save_lead(s, lead, source="t")
        create_user(s, "alice", "pw-12345678")
        create_user(s, "bob", "pw-12345678")
    with session_scope(pg_settings) as s:
        a = s.scalar(select(UserRow.id).where(UserRow.username == "alice"))
        b = s.scalar(select(UserRow.id).where(UserRow.username == "bob"))
    return a, b


def test_pg_concurrent_claim_same_filter_disjoint(pg_settings: Settings) -> None:
    """두 직원이 **같은 필터**로 동시 claim — SKIP LOCKED 로 행겹침 0(조인 있어도 회귀 없음)."""
    a, b = _seed_filtered(pg_settings)
    sm = get_sessionmaker(pg_settings)
    sa, sb = sm(), sm()
    try:
        # alice 의 트랜잭션이 열린(미커밋) 상태에서 bob 가 같은 US 풀을 claim → 잠긴 행 건너뜀.
        ai = claim_work(sa, a, target=4, ttl_minutes=30, now=_T0, countries=["US"], listed="listed")
        bi = claim_work(sb, b, target=4, ttl_minutes=30, now=_T0, countries=["US"], listed="listed")
        sa.commit()
        sb.commit()
    finally:
        sa.close()
        sb.close()
    aids = {it["id"] for it in ai}
    bids = {it["id"] for it in bi}
    assert not (aids & bids)  # 행겹침 0 — of=ReviewQueueRow SKIP LOCKED 가 조인하에서도 작동.
    assert len(ai) == 4 and len(bi) == 2  # US/listed 6건을 4 + 2 로 분할(중복 없음).
    assert all(it["country"] == "US" for it in ai + bi)


def test_pg_concurrent_claim_different_filters(pg_settings: Settings) -> None:
    """두 직원이 **다른 필터**로 동시 claim — 각자 범위만, 잠금 충돌/에러 없음."""
    a, b = _seed_filtered(pg_settings)
    sm = get_sessionmaker(pg_settings)
    sa, sb = sm(), sm()
    try:
        ai = claim_work(sa, a, target=10, ttl_minutes=30, now=_T0, countries=["KR"])
        bi = claim_work(sb, b, target=10, ttl_minutes=30, now=_T0, countries=["US"])
        sa.commit()
        sb.commit()
    finally:
        sa.close()
        sb.close()
    assert {it["country"] for it in ai} == {"KR"} and len(ai) == 4
    assert {it["country"] for it in bi} == {"US"} and len(bi) == 6
    assert not ({it["id"] for it in ai} & {it["id"] for it in bi})
