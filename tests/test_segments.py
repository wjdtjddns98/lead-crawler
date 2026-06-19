"""세그먼트 제너레이터 테스트 — 다국가 발견 구동(곱집합·기본값)."""

from __future__ import annotations

from leadcrawler.sources.countries import supported_countries
from leadcrawler.sources.segments import generate_segments


def test_default_countries_is_all_supported() -> None:
    segs = generate_segments(["건설"])
    iso2 = {c.iso2 for c in supported_countries()}
    # 기본값(countries=None) → 지원 전체국 × 업종 1개.
    assert {s.country for s in segs} == iso2
    assert len(segs) == len(iso2)
    assert all(s.listed == "unknown" for s in segs)


def test_cartesian_product_and_order() -> None:
    segs = generate_segments(["건설", "반도체"], countries=["PH", "TH"])
    assert len(segs) == 4  # 2 국가 × 2 업종.
    # 국가→업종 순서 보존.
    assert (segs[0].country, segs[0].industry) == ("PH", "건설")
    assert (segs[1].country, segs[1].industry) == ("PH", "반도체")
    assert (segs[2].country, segs[2].industry) == ("TH", "건설")


def test_listed_states_multiply() -> None:
    segs = generate_segments(["건설"], countries=["US"], listed=["listed", "unlisted"])
    assert {s.listed for s in segs} == {"listed", "unlisted"}
    assert len(segs) == 2


def test_blank_inputs_are_dropped() -> None:
    segs = generate_segments(["건설", "  ", ""], countries=["PH", ""])
    assert len(segs) == 1  # 빈 업종·국가 제거 → 1 × 1.
    assert segs[0].country == "PH" and segs[0].industry == "건설"
