"""near_dup 매칭 — 블로킹·티어 분류·결정성 테스트(네트워크 없음)."""

from __future__ import annotations

from leadcrawler.dedup_resolve.near_dup import (
    CompanyRecord,
    find_duplicate_candidates,
    match_records,
)


def _rec(key: str, name: str, *, country: str = "KR", domain: str | None = None) -> CompanyRecord:
    return CompanyRecord(key=key, name=name, country=country, domain=domain)


def test_auto_tier_name_high_and_domain_match() -> None:
    # 이름 高(법인격 제거 후 동일) + 도메인root 일치 → auto(자동제거 후보).
    recs = [
        _rec("dom:acme.com", "Acme Corporation", domain="https://acme.com"),
        _rec("name:kr:acme", "Acme, Inc.", domain="http://www.acme.com/ir"),
    ]
    cands = find_duplicate_candidates(recs)
    assert len(cands) == 1
    c = cands[0]
    assert c.tier == "auto"
    assert c.name_score >= 90
    assert c.domain_a == "acme.com" and c.domain_b == "acme.com"
    # key 사전순으로 a<b 고정.
    assert c.key_a < c.key_b


def test_domain_tier_same_domain_diff_name() -> None:
    # 도메인root 일치하나 이름 상이 → domain(쇼트리스트).
    recs = [
        _rec("a", "Alpha Foods", domain="shared.com"),
        _rec("b", "Beta Mining", domain="shared.com"),
    ]
    cands = find_duplicate_candidates(recs)
    assert len(cands) == 1
    assert cands[0].tier == "domain"


def test_keep_both_same_name_diff_domain() -> None:
    # 이름 高이나 도메인 명백히 상이 → keep_both(동명이인 가능, 둘 다 유지).
    recs = [
        _rec("a", "Sunrise Corp", domain="sunrise.co.kr"),
        _rec("b", "Sunrise Corp", domain="sunrise.us"),
    ]
    cands = find_duplicate_candidates(recs)
    assert len(cands) == 1
    assert cands[0].tier == "keep_both"


def test_lexical_tier_name_high_domain_unknown() -> None:
    # 이름 高·도메인 불명(둘 다 없음) → lexical(쇼트리스트).
    recs = [
        _rec("a", "Hanwha Systems"),
        _rec("b", "Hanwha Systems"),
    ]
    cands = find_duplicate_candidates(recs)
    assert len(cands) == 1
    assert cands[0].tier == "lexical"
    assert cands[0].domain_a is None and cands[0].domain_b is None


def test_shortlist_via_thresholds() -> None:
    # 이름 中(strong 미만·medium 이상) → shortlist. 임계값으로 결정적으로 유도.
    recs = [_rec("a", "Hanwha Systems"), _rec("b", "Hanwha Systems")]
    cands = find_duplicate_candidates(recs, name_strong=101, name_medium=90)
    assert len(cands) == 1
    assert cands[0].tier == "shortlist"


def test_no_candidate_low_name_diff_domain() -> None:
    # 이름도 낮고 도메인도 다름 → 같은 기업 근거 없음(후보 아님).
    recs = [
        _rec("a", "Alpha Foods", domain="alpha.com"),
        _rec("b", "Beta Mining", domain="beta.com"),
    ]
    assert find_duplicate_candidates(recs) == []


def test_below_medium_threshold_drops_pair() -> None:
    # 같은 prefix 로 블록되지만 medium 임계를 높이면 후보에서 탈락(도메인 불명 경로).
    recs = [_rec("a", "Hanwha Systems"), _rec("b", "Hanwha Defense")]
    assert find_duplicate_candidates(recs, name_medium=99, name_strong=99.5) == []


def test_blocking_avoids_cross_country() -> None:
    # 국가가 다르면 같은 블록이 아니라 비교조차 하지 않는다.
    recs = [
        _rec("a", "Acme", country="KR"),
        _rec("b", "Acme", country="US"),
    ]
    assert find_duplicate_candidates(recs) == []


def test_deterministic_order() -> None:
    # 입력 순서가 바뀌어도 동일한 결과(결정적 + key 정규화).
    recs = [
        _rec("dom:acme.com", "Acme Corporation", domain="acme.com"),
        _rec("name:kr:acme", "Acme Inc", domain="www.acme.com"),
        _rec("a", "Sunrise Corp", domain="sunrise.co.kr"),
        _rec("b", "Sunrise Corp", domain="sunrise.us"),
    ]
    forward = [(c.key_a, c.key_b, c.tier, c.name_score) for c in find_duplicate_candidates(recs)]
    backward = [
        (c.key_a, c.key_b, c.tier, c.name_score)
        for c in find_duplicate_candidates(list(reversed(recs)))
    ]
    assert forward == backward
    assert forward  # 비어있지 않음(auto + keep_both)


def test_match_records_skips_oversized_block() -> None:
    # 같은 도메인을 공유하는 대형 블록은 캡 초과 시 비교 생략 + skipped_blocks 로 보고.
    recs = [
        _rec(f"k{i}", f"{name} Co", domain="shared.com")
        for i, name in enumerate(["Aaa", "Bbb", "Ccc", "Ddd", "Eee"])
    ]
    res = match_records(recs, max_block_size=3)
    assert res.candidates == []  # 생략돼 후보 없음(조용히 버리지 않음)
    assert len(res.skipped_blocks) == 1
    sb = res.skipped_blocks[0]
    assert sb.kind == "dom" and sb.bucket == "shared.com" and sb.size == 5
    # 캡을 넉넉히 주면 같은 데이터가 후보를 만든다(생략은 캡 때문임을 확인).
    res_full = match_records(recs, max_block_size=100)
    assert res_full.candidates  # 도메인 공유 → domain 티어 후보 생성
    assert res_full.skipped_blocks == []
