"""발견 소스 어댑터 + 레지스트리 테스트(dry_run, 네트워크 없음)."""

from __future__ import annotations

import pytest

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.dart import DartSource
from leadcrawler.sources.edgar import EdgarSource
from leadcrawler.sources.registry import build_sources, discover_segment
from leadcrawler.sources.search import SearchSource


def _dry_settings(**over: object) -> Settings:
    """dry_run 기본 설정(필요 시 필드 override)."""
    return Settings(dry_run=True, **over)


def test_applies_to_country_routing() -> None:
    s = _dry_settings()
    kr = Segment(country="KR", industry="건설")
    us = Segment(country="US", industry="건설")
    assert DartSource(s).applies_to(kr) and not DartSource(s).applies_to(us)
    assert EdgarSource(s).applies_to(us) and not EdgarSource(s).applies_to(kr)
    # 검색 소스는 전 세그먼트 적용.
    assert SearchSource(s).applies_to(kr) and SearchSource(s).applies_to(us)


def test_dry_run_sources_are_deterministic() -> None:
    s = _dry_settings()
    seg = Segment(country="KR", industry="건설")
    first = DartSource(s).discover(seg)
    second = DartSource(s).discover(seg)
    assert first and [c.canonical_key for c in first] == [c.canonical_key for c in second]
    # DART 더미는 registry_id 기반 key 를 쓴다(제약 ① 안정성).
    assert all(c.canonical_key.startswith("reg:dart:") for c in first)


def test_discover_segment_merges_applicable_sources() -> None:
    seg = Segment(country="KR", industry="건설")
    rows = discover_segment(seg, _dry_settings())
    sources = {r.source for r in rows}
    # KR 세그먼트: DART + 검색은 적용, EDGAR(미국)는 미적용.
    assert sources == {"dart", "search"}
    # 병합 후 canonical_key 는 중복이 없어야 한다(제약 ①).
    keys = [r.canonical_key for r in rows]
    assert len(keys) == len(set(keys))


# (소스, 적용 세그먼트, 키 없는 설정 override, 키 있는 설정 override)
_LIVE_CASES = [
    (DartSource, Segment(country="KR", industry="건설"), {}, {"dart_api_key": "k"}),
    (EdgarSource, Segment(country="US", industry="건설"), {}, {"edgar_user_agent": "ua"}),
    (
        SearchSource,
        Segment(country="KR", industry="건설"),
        {},
        {"google_cse_key": "k", "google_cse_cx": "cx"},
    ),
]


@pytest.mark.parametrize(("cls", "seg", "no_key", "with_key"), _LIVE_CASES)
def test_live_branch_without_key_is_noop(cls, seg, no_key, with_key) -> None:  # noqa: ARG001
    # dry_run=False 이고 키가 없으면 네트워크 없이 빈 목록(no-op).
    assert cls(Settings(dry_run=False, **no_key)).discover(seg) == []


def test_search_has_key_compound_logic() -> None:
    seg = Segment(country="KR", industry="건설")
    # google_cse_key 만 있고 cx 가 없으면 키 미충족 → no-op.
    assert SearchSource(Settings(dry_run=False, google_cse_key="k")).discover(seg) == []
    # bing 단독 키만으로는 라이브 미동작(Bing API 폐기) → no-op.
    assert SearchSource(Settings(dry_run=False, bing_api_key="b")).discover(seg) == []


def test_build_sources_registers_all_adapters() -> None:
    names = {src.name for src in build_sources(_dry_settings())}
    assert names == {"edgar", "dart", "search"}
