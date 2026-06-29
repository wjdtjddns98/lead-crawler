"""검증 큐 당겨가기(claim) 동시성 테스트 — 6명 동시 검증 충돌 방지(네트워크 0).

배타 배정(disjoint)·자동 리필·TTL 복귀·반납·충돌 백스톱(409)을 검증한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

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
from leadcrawler.schema import UserRow
from leadcrawler.security import create_user
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import save_lead
from leadcrawler.storage.review import (
    CONFIRMED,
    ReviewConflict,
    claim_work,
    count_reviews,
    query_reviews,
    release_my_claims,
    set_review_status,
)

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
_N = 20


def _seed(settings, n: int) -> None:
    with session_scope(settings) as s:
        for i in range(n):
            lead = CompanyLead(
                company=Company(
                    canonical_key=f"dom:c{i}.com", name=f"C{i:02d}", country="KR",
                    industry="건설", domain=f"c{i}.com", homepage=f"https://c{i}.com",
                    is_active=True, site_alive=True,
                ),
                email=Contact(type=ContactType.EMAIL, value=f"ir@c{i}.com", role=EmailRole.IR),
                email_validation=EmailValidation(status=ValidationStatus.VALID, mx=True),
            )
            save_lead(s, lead, source="t")
        create_user(s, "alice", "pw-12345678")
        create_user(s, "bob", "pw-12345678")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/claim.db")
    get_settings.cache_clear()
    s = get_settings()
    init_db(s)
    _seed(s, _N)
    return s


def _uid(settings, name: str) -> str:
    with session_scope(settings) as s:
        return s.scalar(select(UserRow.id).where(UserRow.username == name))


def test_claim_tops_up_to_target(settings) -> None:
    a = _uid(settings, "alice")
    with session_scope(settings) as s:
        items = claim_work(s, a, target=15, ttl_minutes=30, now=_T0)
    assert len(items) == 15


def test_two_users_get_disjoint_sets(settings) -> None:
    a, b = _uid(settings, "alice"), _uid(settings, "bob")
    with session_scope(settings) as s:
        ai = claim_work(s, a, target=15, ttl_minutes=30, now=_T0)
    with session_scope(settings) as s:
        bi = claim_work(s, b, target=15, ttl_minutes=30, now=_T0)
    aids = {it["id"] for it in ai}
    bids = {it["id"] for it in bi}
    assert not (aids & bids)  # 겹치는 항목 0 — 충돌 구조적 차단.
    assert len(ai) == 15 and len(bi) == 5  # 20건 중 alice 15 + bob 5.


def test_idempotent_topup(settings) -> None:
    a = _uid(settings, "alice")
    with session_scope(settings) as s:
        first = {it["id"] for it in claim_work(s, a, target=15, ttl_minutes=30, now=_T0)}
    with session_scope(settings) as s:
        again = {it["id"] for it in claim_work(s, a, target=15, ttl_minutes=30, now=_T0)}
    assert first == again  # 반복 호출은 같은 15건 유지(과다 점유 없음).


def test_refill_after_confirm(settings) -> None:
    a = _uid(settings, "alice")
    with session_scope(settings) as s:
        ai = claim_work(s, a, target=15, ttl_minutes=30, now=_T0)
        rid = ai[0]["id"]
        set_review_status(
            s, rid, CONFIRMED, assignee="alice", assignee_id=a, claim_ttl_minutes=30, now=_T0
        )
    with session_scope(settings) as s:
        ai2 = claim_work(s, a, target=15, ttl_minutes=30, now=_T0)
    assert len(ai2) == 15  # 확정으로 줄어든 만큼 새 항목 자동 리필.
    assert rid not in {it["id"] for it in ai2}  # 확정 항목은 작업분에서 빠짐.


def test_release_returns_to_pool(settings) -> None:
    a, b = _uid(settings, "alice"), _uid(settings, "bob")
    with session_scope(settings) as s:
        claim_work(s, a, target=20, ttl_minutes=30, now=_T0)  # alice 전부 점유.
    with session_scope(settings) as s:
        assert claim_work(s, b, target=15, ttl_minutes=30, now=_T0) == []  # bob 가져갈 것 없음.
    with session_scope(settings) as s:
        assert release_my_claims(s, a) == 20  # alice 반납.
    with session_scope(settings) as s:
        bi = claim_work(s, b, target=15, ttl_minutes=30, now=_T0)
    assert len(bi) == 15  # 반납분을 bob 가 가져감.


def test_expired_claim_reclaimable(settings) -> None:
    a, b = _uid(settings, "alice"), _uid(settings, "bob")
    with session_scope(settings) as s:
        claim_work(s, a, target=20, ttl_minutes=30, now=_T0)  # alice 전부 점유.
    with session_scope(settings) as s:  # 10분 뒤 — 아직 활성, bob 못 가져감.
        assert claim_work(s, b, target=15, ttl_minutes=30, now=_T0 + timedelta(minutes=10)) == []
    with session_scope(settings) as s:  # 31분 뒤 — TTL 만료, bob 가 가져감.
        bi = claim_work(s, b, target=15, ttl_minutes=30, now=_T0 + timedelta(minutes=31))
    assert len(bi) == 15


def test_conflict_when_other_claims(settings) -> None:
    a, b = _uid(settings, "alice"), _uid(settings, "bob")
    with session_scope(settings) as s:
        rid = claim_work(s, a, target=1, ttl_minutes=30, now=_T0)[0]["id"]
    with session_scope(settings) as s:  # bob 가 alice 점유 항목 처리 시도 → 충돌.
        with pytest.raises(ReviewConflict):
            set_review_status(
                s, rid, CONFIRMED, assignee="bob", assignee_id=b,
                claim_ttl_minutes=30, now=_T0,
            )
    with session_scope(settings) as s:  # 점유자 본인은 처리 가능.
        item = set_review_status(
            s, rid, CONFIRMED, assignee="alice", assignee_id=a, claim_ttl_minutes=30, now=_T0
        )
    assert item["status"] == "confirmed"


# ── 작업범위 필터(국가/업종/상장) — Filtered Claim ──────────────────────────────

_MIXED = [
    # (domain, country, industry, listed)
    ("k1.com", "KR", "건설", Listed.UNKNOWN),
    ("k2.com", "KR", "건설", Listed.UNKNOWN),
    ("u1.com", "US", "Finance", Listed.LISTED),
    ("u2.com", "US", "Finance", Listed.LISTED),
    ("u3.com", "US", "Finance", Listed.UNLISTED),
]


def _seed_mixed(settings) -> None:
    with session_scope(settings) as s:
        for dom, country, industry, listed in _MIXED:
            lead = CompanyLead(
                company=Company(
                    canonical_key=f"dom:{dom}", name=dom, country=country, industry=industry,
                    domain=dom, homepage=f"https://{dom}", is_active=True, site_alive=True,
                    listed=listed,
                ),
                email=Contact(type=ContactType.EMAIL, value=f"ir@{dom}", role=EmailRole.IR),
                email_validation=EmailValidation(status=ValidationStatus.VALID, mx=True),
            )
            save_lead(s, lead, source="t")
        create_user(s, "carol", "pw-12345678")


@pytest.fixture
def mixed_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/mixed.db")
    get_settings.cache_clear()
    s = get_settings()
    init_db(s)
    _seed_mixed(s)
    return s


def test_claim_filter_by_country_alias(mixed_settings) -> None:
    """국가 필터는 별칭·대소문자를 흡수한다('미국'→US 3건만)."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        items = claim_work(s, c, target=10, ttl_minutes=30, now=_T0, countries=["미국"])
    assert {it["country"] for it in items} == {"US"}
    assert len(items) == 3  # u1/u2/u3, KR 제외.


def test_claim_filter_by_industry_case_insensitive(mixed_settings) -> None:
    """업종 필터는 대소문자 무시('finance'→'Finance' 매칭)."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        items = claim_work(s, c, target=10, ttl_minutes=30, now=_T0, industries=["finance"])
    assert {it["industry"] for it in items} == {"Finance"}
    assert len(items) == 3


def test_claim_filter_by_listed_join(mixed_settings) -> None:
    """상장 필터는 DiscoveredCompanyRow 조인으로 동작(listed 만 2건)."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        items = claim_work(s, c, target=10, ttl_minutes=30, now=_T0, listed="listed")
    assert {it["name"] for it in items} == {"u1.com", "u2.com"}  # listed 만.
    assert len(items) == 2  # u3(unlisted)·KR(unknown) 제외.


def test_claim_filter_combined(mixed_settings) -> None:
    """국가+업종+상장 동시 필터 — US/Finance/listed 2건."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        items = claim_work(
            s, c, target=10, ttl_minutes=30, now=_T0,
            countries=["US"], industries=["Finance"], listed="listed",
        )
    assert len(items) == 2 and {it["country"] for it in items} == {"US"}


def test_claim_empty_filter_is_all(mixed_settings) -> None:
    """빈 필터 = 현행 전체 동작(회귀 0) — 5건 전부."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        items = claim_work(s, c, target=10, ttl_minutes=30, now=_T0)
    assert len(items) == 5


def test_claim_filter_switch_releases_non_matching(mixed_settings) -> None:
    """필터를 바꾸면 이전 범위의 비매칭 점유가 자동 반납돼 화면엔 새 범위만 남는다."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        kr = claim_work(s, c, target=10, ttl_minutes=30, now=_T0, countries=["KR"])
    assert {it["country"] for it in kr} == {"KR"} and len(kr) == 2
    # 같은 직원이 US 로 범위 전환 → KR 점유는 반납되고 US 만 남는다.
    with session_scope(mixed_settings) as s:
        us = claim_work(s, c, target=10, ttl_minutes=30, now=_T0, countries=["US"])
    assert {it["country"] for it in us} == {"US"} and len(us) == 3
    # 전환 후 carol 의 활성 점유는 US 3건뿐 — 반납된 KR 은 풀로 복귀(전체 필터로 확인).
    with session_scope(mixed_settings) as s:
        mine = {r["name"] for r in query_reviews(s) if r["status"] == "pending"}
        # KR 2건이 풀에 pending 으로 남아 있어야(반납됨), carol 점유는 US 만.
        assert {"k1.com", "k2.com"} <= mine


def test_invalid_listed_raises_at_storage(mixed_settings) -> None:
    """스토리지 계층도 잘못된 listed 값은 fail-loud(조용한 0건 방지) — 비API 직접 호출 방어."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        with pytest.raises(ValueError):
            claim_work(s, c, target=10, ttl_minutes=30, now=_T0, listed="LISTED")
    with session_scope(mixed_settings) as s:
        with pytest.raises(ValueError):
            count_reviews(s, listed="bogus")


def test_count_and_query_reviews_with_filter(mixed_settings) -> None:
    """count_reviews·query_reviews 도 동일 필터를 반영한다(잔여건수·목록 일관)."""
    with session_scope(mixed_settings) as s:
        assert count_reviews(s, countries=["US"]) == 3
        assert count_reviews(s, listed="listed") == 2
        assert count_reviews(s) == 5  # 빈 필터=전체.
        rows = query_reviews(s, countries=["KR"])
    assert {r["country"] for r in rows} == {"KR"} and len(rows) == 2


def test_no_conflict_when_claim_expired(settings) -> None:
    a, b = _uid(settings, "alice"), _uid(settings, "bob")
    with session_scope(settings) as s:
        rid = claim_work(s, a, target=1, ttl_minutes=30, now=_T0)[0]["id"]
    with session_scope(settings) as s:  # TTL 만료 후엔 다른 직원도 처리 가능(점유 무효).
        item = set_review_status(
            s, rid, CONFIRMED, assignee="bob", assignee_id=b,
            claim_ttl_minutes=30, now=_T0 + timedelta(minutes=31),
        )
    assert item["status"] == "confirmed"
