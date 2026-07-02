"""검증 큐 당겨가기(claim) 동시성 테스트 — 6명 동시 검증 충돌 방지(네트워크 0).

영구 배정 모델: 배타 배정(disjoint)·자동 리필·영구 귀속(시간 경과 무관)·관리자 회수·
전체큐 점유 숨김·충돌 백스톱(409)을 검증한다.
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
from leadcrawler.schema import ReviewAuditRow, UserRow
from leadcrawler.security import create_user
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import save_lead
from leadcrawler.storage.review import (
    CONFIRMED,
    ReviewConflict,
    admin_reclaim,
    claim_work,
    count_reviews,
    query_reviews,
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
        items = claim_work(s, a, target=15, now=_T0)
    assert len(items) == 15


def test_two_users_get_disjoint_sets(settings) -> None:
    a, b = _uid(settings, "alice"), _uid(settings, "bob")
    with session_scope(settings) as s:
        ai = claim_work(s, a, target=15, now=_T0)
    with session_scope(settings) as s:
        bi = claim_work(s, b, target=15, now=_T0)
    aids = {it["id"] for it in ai}
    bids = {it["id"] for it in bi}
    assert not (aids & bids)  # 겹치는 항목 0 — 충돌 구조적 차단.
    assert len(ai) == 15 and len(bi) == 5  # 20건 중 alice 15 + bob 5.


def test_idempotent_topup(settings) -> None:
    a = _uid(settings, "alice")
    with session_scope(settings) as s:
        first = {it["id"] for it in claim_work(s, a, target=15, now=_T0)}
    with session_scope(settings) as s:
        again = {it["id"] for it in claim_work(s, a, target=15, now=_T0)}
    assert first == again  # 반복 호출은 같은 15건 유지(과다 점유 없음).


def test_refill_after_confirm(settings) -> None:
    a = _uid(settings, "alice")
    with session_scope(settings) as s:
        ai = claim_work(s, a, target=15, now=_T0)
        rid = ai[0]["id"]
        set_review_status(s, rid, CONFIRMED, assignee="alice", assignee_id=a, now=_T0)
    with session_scope(settings) as s:
        ai2 = claim_work(s, a, target=15, now=_T0)
    assert len(ai2) == 15  # 확정으로 줄어든 만큼 새 항목 자동 리필.
    assert rid not in {it["id"] for it in ai2}  # 확정 항목은 작업분에서 빠짐.


def test_claims_are_permanent_no_ttl(settings) -> None:
    """점유는 영구 귀속 — 시간이 아무리 지나도 타인이 가져가지 못한다(TTL 복귀 폐기)."""
    a, b = _uid(settings, "alice"), _uid(settings, "bob")
    with session_scope(settings) as s:
        claim_work(s, a, target=20, now=_T0)  # alice 전부 점유.
    with session_scope(settings) as s:  # 하루 뒤에도 bob 가져갈 것 없음.
        assert claim_work(s, b, target=15, now=_T0 + timedelta(days=1)) == []


def test_admin_reclaim_returns_to_pool(settings) -> None:
    """관리자 회수가 유일한 점유 해제 — 회수분은 다른 직원이 받고, 감사행이 남는다."""
    a, b = _uid(settings, "alice"), _uid(settings, "bob")
    with session_scope(settings) as s:
        claim_work(s, a, target=20, now=_T0)  # alice 전부 점유.
    with session_scope(settings) as s:
        assert admin_reclaim(s, a, actor_id=b, actor_username="bob", now=_T0) == 20
    with session_scope(settings) as s:
        bi = claim_work(s, b, target=15, now=_T0)
    assert len(bi) == 15  # 회수분을 bob 가 가져감.
    with session_scope(settings) as s:  # 회수 감사행(action="reclaim") 적재 확인.
        n = len(
            s.scalars(
                select(ReviewAuditRow.id).where(ReviewAuditRow.action == "reclaim")
            ).all()
        )
    assert n == 20


def test_conflict_when_other_claims(settings) -> None:
    a, b = _uid(settings, "alice"), _uid(settings, "bob")
    with session_scope(settings) as s:
        rid = claim_work(s, a, target=1, now=_T0)[0]["id"]
    with session_scope(settings) as s:  # bob 가 alice 점유 항목 처리 시도 → 충돌.
        with pytest.raises(ReviewConflict):
            set_review_status(s, rid, CONFIRMED, assignee="bob", assignee_id=b, now=_T0)
    with session_scope(settings) as s:  # 점유자 본인은 처리 가능.
        item = set_review_status(s, rid, CONFIRMED, assignee="alice", assignee_id=a, now=_T0)
    assert item["status"] == "confirmed"


def test_conflict_persists_over_time(settings) -> None:
    """영구 배정 — 오래 지나도 타인 처리 시도는 여전히 409(기존 TTL 관용 폐기)."""
    a, b = _uid(settings, "alice"), _uid(settings, "bob")
    with session_scope(settings) as s:
        rid = claim_work(s, a, target=1, now=_T0)[0]["id"]
    with session_scope(settings) as s:
        with pytest.raises(ReviewConflict):
            set_review_status(
                s, rid, CONFIRMED, assignee="bob", assignee_id=b,
                now=_T0 + timedelta(days=7),
            )


def test_claimed_hidden_from_full_queue(settings) -> None:
    """점유된 항목은 전체큐 목록·건수에서 숨는다(전체큐 = 미점유 작업만)."""
    a = _uid(settings, "alice")
    with session_scope(settings) as s:
        assert count_reviews(s) == _N
        claim_work(s, a, target=15, now=_T0)
    with session_scope(settings) as s:
        assert count_reviews(s) == _N - 15  # 점유 15건 제외.
        assert len(query_reviews(s, limit=100)) == _N - 15
        assert count_reviews(s, status="pending") == _N - 15


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
        items = claim_work(s, c, target=10, now=_T0, countries=["미국"])
    assert {it["country"] for it in items} == {"US"}
    assert len(items) == 3  # u1/u2/u3, KR 제외.


def test_claim_filter_by_industry_case_insensitive(mixed_settings) -> None:
    """업종 필터는 대소문자 무시('finance'→'Finance' 매칭)."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        items = claim_work(s, c, target=10, now=_T0, industries=["finance"])
    assert {it["industry"] for it in items} == {"Finance"}
    assert len(items) == 3


def test_claim_filter_by_listed_join(mixed_settings) -> None:
    """상장 필터는 DiscoveredCompanyRow 조인으로 동작(listed 만 2건)."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        items = claim_work(s, c, target=10, now=_T0, listed="listed")
    assert {it["name"] for it in items} == {"u1.com", "u2.com"}  # listed 만.
    assert len(items) == 2  # u3(unlisted)·KR(unknown) 제외.


def test_claim_filter_combined(mixed_settings) -> None:
    """국가+업종+상장 동시 필터 — US/Finance/listed 2건."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        items = claim_work(
            s, c, target=10, now=_T0,
            countries=["US"], industries=["Finance"], listed="listed",
        )
    assert len(items) == 2 and {it["country"] for it in items} == {"US"}


def test_claim_empty_filter_is_all(mixed_settings) -> None:
    """빈 필터 = 현행 전체 동작(회귀 0) — 5건 전부."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        items = claim_work(s, c, target=10, now=_T0)
    assert len(items) == 5


def test_claim_filter_switch_keeps_claims(mixed_settings) -> None:
    """필터는 신규 배정에만 적용 — 필터를 바꿔도 기존 점유는 반납되지 않고 유지된다."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        kr = claim_work(s, c, target=10, now=_T0, countries=["KR"])
    assert {it["country"] for it in kr} == {"KR"} and len(kr) == 2
    # 같은 직원이 US 로 범위 전환 → KR 점유 유지 + US 신규 배정(혼합 작업분).
    with session_scope(mixed_settings) as s:
        mine = claim_work(s, c, target=10, now=_T0, countries=["US"])
    assert {it["country"] for it in mine} == {"KR", "US"} and len(mine) == 5
    with session_scope(mixed_settings) as s:  # 전체큐엔 아무것도 안 남음(전부 carol 점유).
        assert count_reviews(s, status="pending") == 0


def test_target_cap_is_global_across_filters(mixed_settings) -> None:
    """target 상한은 필터 무관 전역 — 받은 걸 처리해야 다음을 받는다(무한 누적 방지)."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        kr = claim_work(s, c, target=3, now=_T0, countries=["KR"])
    assert len(kr) == 2  # KR 은 2건뿐.
    with session_scope(mixed_settings) as s:  # US 로 전환해도 총량 3 상한 → 1건만 추가.
        mine = claim_work(s, c, target=3, now=_T0, countries=["US"])
    assert len(mine) == 3


def test_invalid_listed_raises_at_storage(mixed_settings) -> None:
    """스토리지 계층도 잘못된 listed 값은 fail-loud(조용한 0건 방지) — 비API 직접 호출 방어."""
    c = _uid(mixed_settings, "carol")
    with session_scope(mixed_settings) as s:
        with pytest.raises(ValueError):
            claim_work(s, c, target=10, now=_T0, listed="LISTED")
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
