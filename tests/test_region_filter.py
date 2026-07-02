"""큐 지역(region) 작업범위 필터 — 저장·카운트·배정·옵션 (네트워크 0, SQLite)."""

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
from leadcrawler.security import create_user
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import save_lead
from leadcrawler.storage.review import (
    claim_work,
    count_reviews,
    list_regions,
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


def _seed(settings) -> str:
    """부산 2·서울 1·지역미상 1 을 시드하고 사용자 id 를 반환한다."""
    regions = ["부산", "부산", "서울", None]
    with session_scope(settings) as s:
        for i, region in enumerate(regions):
            lead = CompanyLead(
                company=Company(
                    canonical_key=f"dom:r{i}.com", name=f"R{i:02d}", country="KR",
                    industry="건설", domain=f"r{i}.com", homepage=f"https://r{i}.com",
                    is_active=True, site_alive=True, listed=Listed.LISTED,
                ),
                email=Contact(type=ContactType.EMAIL, value=f"ir@r{i}.com", role=EmailRole.IR),
                email_validation=EmailValidation(status=ValidationStatus.VALID, mx=True),
            )
            save_lead(s, lead, source="t")
            row = s.get(DiscoveredCompanyRow, f"dom:r{i}.com")
            row.region = region
        user = create_user(s, "alice", "pw-12345678")
        uid = user.id
    return uid


def test_count_and_query_by_region(settings) -> None:
    _seed(settings)
    with session_scope(settings) as s:
        assert count_reviews(s, regions=["부산"]) == 2
        assert count_reviews(s, regions=["서울"]) == 1
        assert count_reviews(s, regions=["부산", "서울"]) == 3
        assert count_reviews(s) == 4  # 필터 없음 = 지역미상 포함 전체.
        names = {it["name"] for it in query_reviews(s, regions=["부산"])}
        assert names == {"R00", "R01"}


def test_region_combines_with_listed(settings) -> None:
    """listed 와 region 이 같은 조인을 공유해도 정상 동작(이중 조인 회귀 방지)."""
    _seed(settings)
    with session_scope(settings) as s:
        assert count_reviews(s, regions=["부산"], listed="listed") == 2
        assert count_reviews(s, regions=["부산"], listed="unlisted") == 0


def test_claim_assigns_only_matching_region(settings) -> None:
    uid = _seed(settings)
    with session_scope(settings) as s:
        items = claim_work(s, uid, batch=30, cap=100, regions=["부산"])
        assert {it["name"] for it in items} == {"R00", "R01"}


def test_list_regions_distinct_sorted(settings) -> None:
    _seed(settings)
    with session_scope(settings) as s:
        assert list_regions(s) == ["부산", "서울"]
