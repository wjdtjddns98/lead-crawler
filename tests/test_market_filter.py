"""큐 시장 보드(market) 작업범위 필터 — 저장·카운트·옵션 (네트워크 0, SQLite).

PR #148(FE 시장 필터) BE 계약: GET /queue market CSV + /queue/filters markets.
POST /queue/claim 은 범위 밖(FE 가 market 을 안 보냄) — claim 테스트는 없다.
"""

from __future__ import annotations

import pytest

from leadcrawler.config import get_settings
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
from leadcrawler.schema import DiscoveredCompanyRow
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import save_lead
from leadcrawler.storage.review import (
    count_reviews,
    list_markets,
    query_reviews,
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("LEADCRAWLER_DRY_RUN", "true")
    get_settings.cache_clear()
    s = get_settings()
    init_db(s)
    yield s
    get_settings.cache_clear()


def _seed(settings) -> None:
    """KOSDAQ 2·KOSPI 1·시장미상 1 을 시드한다."""
    markets = ["KOSDAQ", "KOSDAQ", "KOSPI", None]
    with session_scope(settings) as s:
        for i, market in enumerate(markets):
            lead = CompanyLead(
                company=Company(
                    canonical_key=f"dom:m{i}.com", name=f"M{i:02d}", country="KR",
                    industry="건설", domain=f"m{i}.com", homepage=f"https://m{i}.com",
                    is_active=True, site_alive=True, listed=Listed.LISTED,
                ),
                email=Contact(type=ContactType.EMAIL, value=f"ir@m{i}.com", role=EmailRole.IR),
                email_validation=EmailValidation(status=ValidationStatus.VALID, mx=True),
            )
            save_lead(s, lead, source="t")
            row = s.get(DiscoveredCompanyRow, f"dom:m{i}.com")
            row.market = market


def test_count_and_query_by_market(settings) -> None:
    _seed(settings)
    with session_scope(settings) as s:
        assert count_reviews(s, markets=["KOSDAQ"]) == 2
        assert count_reviews(s, markets=["KOSPI"]) == 1
        assert count_reviews(s, markets=["KOSDAQ", "KOSPI"]) == 3
        assert count_reviews(s) == 4  # 필터 없음 = 시장미상 포함 전체.
        names = {it["name"] for it in query_reviews(s, markets=["KOSDAQ"])}
        assert names == {"M00", "M01"}


def test_market_input_case_insensitive(settings) -> None:
    """저장값은 대문자 — 소문자 입력('kosdaq')도 upper() 정규화로 매칭돼야 한다."""
    _seed(settings)
    with session_scope(settings) as s:
        assert count_reviews(s, markets=["kosdaq"]) == 2


def test_market_combines_with_listed(settings) -> None:
    """listed 와 market 이 같은 조인을 공유해도 정상 동작(이중 조인 회귀 방지)."""
    _seed(settings)
    with session_scope(settings) as s:
        assert count_reviews(s, markets=["KOSDAQ"], listed="listed") == 2
        assert count_reviews(s, markets=["KOSDAQ"], listed="unlisted") == 0


def test_list_markets_distinct_sorted(settings) -> None:
    _seed(settings)
    with session_scope(settings) as s:
        assert list_markets(s) == ["KOSDAQ", "KOSPI"]
