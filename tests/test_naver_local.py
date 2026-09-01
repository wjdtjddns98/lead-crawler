"""네이버 지역검색 소스 — FakeFetcher 로 네트워크 없이 계약 검증."""

from __future__ import annotations

from typing import Any

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.naver_local import NaverLocalSource, _industry_terms


class FakeFetcher:
    def __init__(self, on_query) -> None:
        self._on_query = on_query
        self.queries: list[str] = []

    def get_json(self, url: str, *, params=None, headers=None) -> Any:
        assert "local.json" in url
        assert headers and headers.get("X-Naver-Client-Id")
        q = (params or {})["query"]
        self.queries.append(q)
        return self._on_query(q)


def _settings(**over) -> Settings:
    kwargs = {"dry_run": False, "naver_client_id": "id", "naver_client_secret": "sec"}
    kwargs.update(over)
    return Settings(**kwargs)


def _item(name: str, link: str = "", address: str = "서울특별시 강남구 x", tel: str = "") -> dict:
    return {"title": f"<b>{name}</b>", "link": link, "address": address, "telephone": tel}


def test_industry_terms_split_and_broad_empty() -> None:
    assert _industry_terms("화학·석유화학") == ["화학", "석유화학"]
    # broad 라벨은 빈 목록 — "전체" 검색은 업종무관 유입(순도 붕괴)이라 소스가 안 돈다(리뷰 M5).
    assert _industry_terms("전체") == []
    assert _industry_terms("") == []


def test_applies_to_kr_only_and_key_gated() -> None:
    src = NaverLocalSource(_settings())
    assert src.applies_to(Segment(country="KR", industry="화학·석유화학"))
    assert not src.applies_to(Segment(country="US", industry="화학·석유화학"))
    # 로컬 .env 의 실키 누출 차단 — 명시 빈값으로 무키를 강제(결정적).
    nokey = NaverLocalSource(
        Settings(dry_run=False, naver_client_id="", naver_client_secret="")
    )
    assert not nokey.applies_to(Segment(country="KR", industry="화학·석유화학"))
    # broad KR 도 안 돈다(업종-우선 소스 — 리뷰 M5).
    assert not src.applies_to(Segment(country="KR", industry="전체"))


def test_live_queries_terms_with_region_prefix_and_emits() -> None:
    def _on_query(q: str) -> dict:
        return {"items": [_item(f"{q} 업체A", link="http://a-{0}.co.kr".format(len(q))),
                          _item(f"{q} 업체B")]}

    f = FakeFetcher(_on_query)
    src = NaverLocalSource(_settings(), fetcher=f)
    seg = Segment(country="KR", industry="화학·석유화학", region="수원")
    out = src.discover(seg)
    assert f.queries == ["수원 화학", "수원 석유화학"]  # 지역 프리픽스 + 라벨 분해.
    names = [d.name for d in out]
    assert "수원 화학 업체A" in names and len(out) == 4  # 쿼리 2 × 2업체, 태그 제거.
    with_domain = [d for d in out if d.domain]
    assert with_domain and all(d.source == "naver_local" for d in out)
    # 주소 → region 파생(KR 시/도) 경로는 build_company 공용 로직(별도 검증 존재).


def test_live_dedups_same_name_across_queries_and_caps() -> None:
    def _on_query(q):  # noqa: ARG001 — 모든 쿼리가 같은 업체 반환.
        return {"items": [_item("중복상사")]}

    f = FakeFetcher(_on_query)
    src = NaverLocalSource(_settings(discovery_max_per_source=1), fetcher=f)
    out = src.discover(Segment(country="KR", industry="화학·석유화학"))
    assert len(out) == 1  # 쿼리 간 업체명 dedup + cap.


def test_dry_run_deterministic_no_network() -> None:
    class Boom:
        def get_json(self, *a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("dry_run 은 네트워크 금지")

    src = NaverLocalSource(Settings(dry_run=True), fetcher=Boom())
    seg = Segment(country="KR", industry="화학·석유화학", region="서울")
    out1 = src.discover(seg)
    out2 = src.discover(seg)
    assert out1 and [d.canonical_key for d in out1] == [d.canonical_key for d in out2]


def test_live_normalizes_link_and_drops_platform_domains() -> None:
    """link 원시 URL → 정규화 저장 + 공유 플랫폼 링크는 도메인 폐기(name: 키 분리 보존).

    원시 URL 이 domain 에 그대로 들어가면 enrich/existence 가 URL 을 호스트명으로 취급해
    전면 실패한다(2026-08-18 실사고, 승격 0). 정규화는 루트까지 접으므로(blog.naver.com/A·/B
    → naver.com) 플랫폼 도메인을 살려두면 서로 다른 업체가 dom:naver.com 한 행에 뭉개져
    영구 유실되고(제약①) 포털 루트가 승격된다 — 폐기(None)해 업체를 분리 보존한다."""
    def _on_query(q):  # noqa: ARG001
        return {"items": [
            _item("정규화상사", link="http://foo.co.kr/sub?x=1"),
            _item("블로그상사A", link="https://blog.naver.com/a"),
            _item("블로그상사B", link="https://blog.naver.com/b"),
            _item("무링크상사"),
        ]}

    src = NaverLocalSource(_settings(), fetcher=FakeFetcher(_on_query))
    out = src.discover(Segment(country="KR", industry="화학·석유화학"))
    domains = {d.name: d.domain for d in out}
    assert domains["정규화상사"] == "foo.co.kr"
    assert domains["블로그상사A"] is None and domains["블로그상사B"] is None  # 플랫폼 폐기.
    assert domains["무링크상사"] is None  # 링크 없음 → None 유지.
    keys = {d.name: d.canonical_key for d in out}
    assert keys["블로그상사A"].startswith("name:")  # 도메인 폐기 → name: 티어 폴백.
    assert keys["블로그상사A"] != keys["블로그상사B"]  # 서로 다른 업체가 분리 보존된다.


def test_applies_to_rejects_listed_scope() -> None:
    """상장 스코프 세그먼트에선 안 돈다 — 지역검색은 상장여부를 판별할 수 없는 소스라
    listed 스코프 크롤에서 돌면 미검증 업체가 그 스코프값으로 저장·오염된다(리뷰 MED)."""
    src = NaverLocalSource(_settings())
    assert not src.applies_to(
        Segment(country="KR", industry="화학·석유화학", listed="listed")
    )
    assert not src.applies_to(
        Segment(country="KR", industry="화학·석유화학", listed="unlisted")
    )


def test_live_unescapes_html_entities_in_name() -> None:
    """title 의 HTML 엔티티 복원 — name: canonical_key 오염 방지(리뷰 M6)."""
    def _on_query(q):  # noqa: ARG001
        return {"items": [_item("AT&amp;T 코리아")]}

    src = NaverLocalSource(_settings(), fetcher=FakeFetcher(_on_query))
    out = src.discover(Segment(country="KR", industry="통신·네트워크"))
    assert any(d.name == "AT&T 코리아" for d in out)
