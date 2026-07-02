"""dedup 확정키(reg_no) 티어 — 등록번호 일치=확정 중복·상이=별개 법인 보호."""

from __future__ import annotations

from leadcrawler.cli import confirmed_pairs_from_report
from leadcrawler.dedup import normalize_reg_no
from leadcrawler.dedup_resolve.near_dup import CompanyRecord, find_duplicate_candidates
from leadcrawler.dedup_resolve.report import build_report


def test_normalize_reg_no() -> None:
    assert normalize_reg_no("124-81-00998") == "1248100998"
    assert normalize_reg_no(" 1248100998 ") == "1248100998"
    assert normalize_reg_no("SC123456") == "sc123456"
    assert normalize_reg_no(None) is None
    assert normalize_reg_no("---") is None


def test_regno_match_confirms_despite_different_name_and_domain() -> None:
    """이름·도메인이 전혀 달라도 같은 국가+등록번호면 확정 티어로 잡힌다(전용 블로킹)."""
    records = [
        CompanyRecord(key="reg:dart:1", name="삼성전자", country="KR",
                      domain="samsung.com", reg_no="124-81-00998"),
        CompanyRecord(key="dom:sec.co.kr", name="에스이씨코리아", country="KR",
                      domain="sec.co.kr", reg_no="1248100998"),
    ]
    cands = find_duplicate_candidates(records)
    assert len(cands) == 1
    assert cands[0].tier == "reg_no"


def test_regno_mismatch_blocks_auto_merge() -> None:
    """이름 高+도메인 일치(원래 auto)라도 등록번호가 다르면 계열사 보호(keep_both)."""
    records = [
        CompanyRecord(key="a", name="Samsung Electronics", country="KR",
                      domain="samsung.com", reg_no="124-81-00998"),
        CompanyRecord(key="b", name="Samsung Electronics", country="KR",
                      domain="samsung.com", reg_no="999-99-99999"),
    ]
    cands = find_duplicate_candidates(records)
    assert len(cands) == 1
    assert cands[0].tier == "keep_both"


def test_regno_missing_falls_back_to_ladder() -> None:
    """한쪽이라도 번호가 없으면 기존 사다리(auto) 그대로."""
    records = [
        CompanyRecord(key="a", name="Samsung Electronics", country="KR",
                      domain="samsung.com", reg_no="124-81-00998"),
        CompanyRecord(key="b", name="Samsung Electronics", country="KR",
                      domain="samsung.com"),
    ]
    cands = find_duplicate_candidates(records)
    assert len(cands) == 1
    assert cands[0].tier == "auto"


def test_regno_different_country_not_confirmed() -> None:
    """국가가 다르면 번호가 같아도 확정 아님(체계 상이 — 오탐 방지)."""
    records = [
        CompanyRecord(key="a", name="Alpha KR", country="KR", reg_no="12345678"),
        CompanyRecord(key="b", name="Beta GB", country="GB", reg_no="12345678"),
    ]
    assert find_duplicate_candidates(records) == []


def test_report_auto_removable_counts_regno_and_merge_pairs_include_it() -> None:
    records = [
        CompanyRecord(key="reg:dart:1", name="가나다", country="KR", reg_no="111-11-11111"),
        CompanyRecord(key="dom:x.kr", name="라마바", country="KR", domain="x.kr",
                      reg_no="1111111111"),
    ]
    rpt = build_report(records)
    assert rpt.by_tier.get("reg_no") == 1
    assert rpt.auto_removable == 1
    pairs = confirmed_pairs_from_report(
        rpt.model_dump(), include_llm=False, min_confidence=0.8
    )
    assert pairs == [("dom:x.kr", "reg:dart:1")]
