"""검색 발견 소스 — 결과 제목→회사명 정제(_clean_name)·지역 쿼리 팬아웃 테스트."""

from __future__ import annotations

import pytest

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.search import SearchSource, _clean_name


@pytest.mark.parametrize(
    "title,domain,expected",
    [
        # IR/네비 문구 조각을 버리고 실제 회사명 조각을 남긴다.
        ("Investor Relations :: Meritage Homes", "meritagehomes.com", "Meritage Homes"),
        ("MasTec: Investor Relations", "mastec.com", "MasTec"),
        ("Investor Relations | Komatsu", "komatsu.jp", "Komatsu"),
        ("Home | Quanta Services, Inc.", "quantaservices.com", "Quanta Services, Inc."),
        ("Investor Relations - CNH Industrial", "cnh.com", "CNH Industrial"),
        # 구분자 없는 깔끔한 제목은 그대로.
        ("Builders FirstSource, Inc.", "bldr.com", "Builders FirstSource, Inc."),
    ],
)
def test_clean_name_extracts_company(title: str, domain: str, expected: str) -> None:
    assert _clean_name(title, domain) == expected


def test_clean_name_all_noise_falls_back_to_domain_root() -> None:
    # 제목이 통째로 IR/네비 문구면 도메인 root 를 제목화한다(쓸 만한 조각 없음).
    assert _clean_name("Investor Relations", "meritagehomes.com") == "Meritagehomes"
    assert _clean_name("Home | Investor Relations", "acme.co") == "Acme"


def test_clean_name_empty_title_uses_domain() -> None:
    assert _clean_name("", "acme.com") == "Acme"


def test_dry_region_segments_yield_distinct_domains() -> None:
    # 지역 세그먼트별 더미 도메인이 달라야 dry 크롤에서 팬아웃이 dedup 에 접히지 않는다.
    # 17개 시/도 전부 + 기본(지역 없음)이 상호 disjoint 여야 한다 — hex 태그를 절단하면
    # 첫 글자가 같은 쌍(충북/충남·전북/전남·경북/경남)이 접히는 회귀를 잡는다.
    from leadcrawler.region import KR_REGIONS

    src = SearchSource(Settings(dry_run=True))
    domain_sets = {
        r: {d.domain for d in src.discover(Segment(country="KR", industry="바이오", region=r))}
        for r in (*KR_REGIONS, None)
    }
    assert all(domain_sets.values())
    all_domains = [d for s in domain_sets.values() for d in s]
    assert len(all_domains) == len(set(all_domains))  # 전 지역×기본 도메인 전역 중복 0.
    # 같은 지역은 결정적(재실행 동일).
    again = src.discover(Segment(country="KR", industry="바이오", region="서울"))
    assert {d.domain for d in again} == domain_sets["서울"]


def test_live_queries_get_region_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # 지역 세그먼트의 라이브 검색 쿼리는 전부 지역 키워드로 시작한다(네트워크 없음 — 캡처만).
    src = SearchSource(Settings(dry_run=False))
    captured: list[str] = []

    def _capture(provider: object, segment: Segment, query: str, **kw: object) -> list:
        captured.append(query)
        return []

    monkeypatch.setattr(src, "_fetch_query", _capture)
    monkeypatch.setattr(src, "_get_provider", lambda: object())
    src.discover(Segment(country="KR", industry="바이오", region="서울"))
    assert captured and all(q.startswith("서울 ") for q in captured)
    # 기본 세그먼트(지역 없음)는 기존 쿼리 그대로.
    captured.clear()
    src.discover(Segment(country="KR", industry="바이오"))
    assert captured and not any(q.startswith("서울 ") for q in captured)
