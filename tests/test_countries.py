"""국가 식별/별칭 테스트 — resolve_country 가 자유표기를 ISO2 로 수렴하는지."""

from __future__ import annotations

import pytest

from leadcrawler.sources.countries import (
    resolve_country,
    supported_countries,
)


@pytest.mark.parametrize(
    ("text", "iso2"),
    [
        # 확장된 영문 공식/축약 표기.
        ("Republic of Korea", "KR"),
        ("ROK", "KR"),
        ("United States of America", "US"),
        ("U.S.A.", "US"),
        ("Deutschland", "DE"),
        ("People's Republic of China", "CN"),
        ("PRC", "CN"),
        ("Kingdom of Thailand", "TH"),
        ("Brasil", "BR"),
        ("United Kingdom of Great Britain and Northern Ireland", "GB"),
        # 현지/한자/한글 표기.
        ("日本", "JP"),
        ("中国", "CN"),
        ("台灣", "TW"),
        ("香港", "HK"),
        ("코리아", "KR"),
        ("오스트레일리아", "AU"),
        # 기존 표기(회귀 0).
        ("KR", "KR"),
        ("대한민국", "KR"),
        ("us", "US"),
    ],
)
def test_resolve_country_recognizes_aliases(text: str, iso2: str) -> None:
    c = resolve_country(text)
    assert c is not None and c.iso2 == iso2


def test_resolve_country_is_case_and_space_insensitive() -> None:
    assert resolve_country("  republic of KOREA ").iso2 == "KR"  # type: ignore[union-attr]


def test_resolve_country_unregistered_returns_none() -> None:
    # 레지스트리에 없는 국가는 None(발견 스코프 불변 — 별칭 확장이 크롤 대상을 늘리지 않음).
    assert resolve_country("Austria") is None
    assert resolve_country("Narnia") is None
    assert resolve_country("") is None


def test_no_duplicate_aliases_across_countries() -> None:
    # 한 별칭이 두 국가에 걸리면 _INDEX 가 조용히 덮어써 오해석 → 중복을 명시 차단.
    seen: dict[str, str] = {}
    for c in supported_countries():
        for alias in c.aliases:
            assert alias == alias.lower(), f"별칭은 소문자여야 함: {alias!r}"
            assert alias not in seen, f"중복 별칭 {alias!r}: {seen[alias]} vs {c.iso2}"
            seen[alias] = c.iso2
