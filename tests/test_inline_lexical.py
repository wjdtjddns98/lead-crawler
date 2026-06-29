"""수집 시점 인라인 렉시컬 중복 후보(갭1) 테스트.

도메인 없는 신규 기업을 기존 name: 티어와 이름 유사도로 대조해 dedup_candidate(pending)
로 적재하되 자동 스킵/머지는 안 함(제약②). conftest 가 격리 SQLite 를 잡는다.
"""

from __future__ import annotations

from leadcrawler.config import get_settings
from leadcrawler.dedup_resolve.inline_lexical import InlineLexicalMatcher
from leadcrawler.schema import DedupCandidateRow, DiscoveredCompanyRow
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.dedup_candidate import pair_id


def test_similar_name_same_country_creates_pending_candidate() -> None:
    init_db(get_settings())
    with session_scope(get_settings()) as s:
        m = InlineLexicalMatcher(s)  # 빈 인덱스로 시작.
        assert m.consider(s, "name:us:acmeglobal", "Acme Global", "US") == 0  # 비교 대상 없음.
        # 유사 이름(오타) — 같은 prefix 블록·국가 → 후보 1건.
        assert m.consider(s, "name:us:acmeglobel", "Acme Globel", "US") == 1
        s.flush()
        cid = pair_id("name:us:acmeglobal", "name:us:acmeglobel")
        row = s.get(DedupCandidateRow, cid)
        assert row is not None and row.status == "pending"
        assert {row.key_a, row.key_b} == {"name:us:acmeglobal", "name:us:acmeglobel"}
        assert row.name_score >= 84.0
        assert "inline" in row.reason


def test_different_country_or_name_no_candidate() -> None:
    init_db(get_settings())
    with session_scope(get_settings()) as s:
        m = InlineLexicalMatcher(s)
        m.consider(s, "name:us:acmeglobal", "Acme Global", "US")
        # 다른 국가(같은 이름) → 블록 분리, 후보 없음(동명이인 다국가 보존).
        assert m.consider(s, "name:kr:acmeglobal", "Acme Global", "KR") == 0
        # 전혀 다른 이름 → 후보 없음.
        assert m.consider(s, "name:us:zenithcorp", "Zenith", "US") == 0
        s.flush()
        assert s.query(DedupCandidateRow).count() == 0


def test_loads_existing_name_tier_rows_on_init() -> None:
    init_db(get_settings())
    with session_scope(get_settings()) as s:
        # 사전 시드된 name: 티어 기업을 init 이 인덱스에 적재해야 신규와 대조된다.
        s.add(DiscoveredCompanyRow(canonical_key="name:us:acmeglobal", name="Acme Global", country="US"))
        s.flush()
        m = InlineLexicalMatcher(s)
        assert m.consider(s, "name:us:acmeglobel", "Acme Globel", "US") == 1


def test_does_not_resurrect_human_decided_pair() -> None:
    init_db(get_settings())
    with session_scope(get_settings()) as s:
        cid = pair_id("name:us:acmeglobal", "name:us:acmeglobel")
        s.add(DedupCandidateRow(
            id=cid, key_a="name:us:acmeglobal", key_b="name:us:acmeglobel",
            tier="lexical", status="separated"))  # 사람이 '분리' 확정함.
        s.flush()
        m = InlineLexicalMatcher(s)
        m.consider(s, "name:us:acmeglobal", "Acme Global", "US")
        assert m.consider(s, "name:us:acmeglobel", "Acme Globel", "US") == 0  # 부활 안 함.
        assert s.get(DedupCandidateRow, cid).status == "separated"  # 결정 보존.
