"""발견 소스 어댑터 + 레지스트리 테스트(dry_run, 네트워크 없음)."""

from __future__ import annotations

import pytest

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.companieshouse import CompaniesHouseSource
from leadcrawler.sources.dart import DartSource
from leadcrawler.sources.edgar import EdgarSource
from leadcrawler.sources.exchanges import (
    BursaSource,
    IdxSource,
    PseSource,
    SetSource,
    SgxSource,
)
from leadcrawler.sources.gleif import GleifSource
from leadcrawler.sources.opencorporates import OpenCorporatesSource
from leadcrawler.sources.registry import build_sources, discover_segment
from leadcrawler.sources.search import SearchSource
from leadcrawler.sources.wikidata import WikidataSource


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
    # 광범위 업종(미매핑 '전체'): 업종 필터를 못 하는 집계원도 적용된다.
    seg = Segment(country="KR", industry="전체")
    rows = discover_segment(seg, _dry_settings())
    sources = {r.source for r in rows}
    # KR 세그먼트: DART + 집계원(GLEIF/Wikidata/OpenCorporates) + 검색 적용,
    # EDGAR(미국)·CompaniesHouse(영국)는 미적용.
    assert sources == {"dart", "gleif", "wikidata", "opencorporates", "search"}
    # 병합 후 canonical_key 는 중복이 없어야 한다(제약 ①).
    keys = [r.canonical_key for r in rows]
    assert len(keys) == len(set(keys))


def test_specific_industry_gates_unfiltered_sources() -> None:
    # 정밀도 우선: 구체 업종(건설) 지정 시 업종 필터를 못 하는 GLEIF/Wikidata 는 빠지고,
    # 등록처(DART)·검색·업종질의 가능한 OpenCorporates 만 남는다.
    rows = discover_segment(Segment(country="KR", industry="건설"), _dry_settings())
    assert {r.source for r in rows} == {"dart", "opencorporates", "search"}


def test_aggregator_applies_to_resolvable_countries_only() -> None:
    s = _dry_settings()
    # 광범위 업종(미매핑 '전체')으로 순수 국가 라우팅만 검증(집계원 업종 게이팅과 분리).
    ph = Segment(country="필리핀", industry="전체")  # 한글 별칭 해석.
    th = Segment(country="TH", industry="전체")  # ISO2.
    unknown = Segment(country="Atlantis", industry="전체")  # 미등록 → 폴백(검색만).
    for cls in (GleifSource, WikidataSource, OpenCorporatesSource):
        assert cls(s).applies_to(ph) and cls(s).applies_to(th)
        assert not cls(s).applies_to(unknown)


def test_specific_industry_gates_aggregators_not_opencorporates() -> None:
    # 구체 업종(제조): GLEIF/Wikidata/거래소는 업종 필터를 못 해 빠지고, 업종을 영어
    # 검색어로 질의하는 OpenCorporates 는 남는다(정밀도 우선).
    s = _dry_settings()
    ph = Segment(country="필리핀", industry="제조")
    assert not GleifSource(s).applies_to(ph)
    assert not WikidataSource(s).applies_to(ph)
    assert not PseSource(s).applies_to(ph)  # 거래소 상장목록도 업종 필터 없음 → 제외.
    assert OpenCorporatesSource(s).applies_to(ph)  # 업종 질의 가능 → 유지.
    # 광범위 업종이면 집계원·거래소도 다시 적용된다.
    broad = Segment(country="필리핀", industry="전체")
    assert GleifSource(s).applies_to(broad) and PseSource(s).applies_to(broad)


def test_unknown_country_routes_to_search_only() -> None:
    # 미등록 국가는 등록처·집계원 모두 미적용 → 검색 소스로만 폴백.
    rows = discover_segment(Segment(country="Atlantis", industry="제조"), _dry_settings())
    assert {r.source for r in rows} == {"search"}


def test_aggregator_dry_run_keys_are_registry_based() -> None:
    s = _dry_settings()
    seg = Segment(country="PH", industry="제조")
    gleif = GleifSource(s).discover(seg)
    wiki = WikidataSource(s).discover(seg)
    assert gleif and all(c.canonical_key.startswith("reg:lei:") for c in gleif)
    assert wiki and all(c.canonical_key.startswith("reg:wikidata:") for c in wiki)
    # 결정적이어야 한다(제약 ① 안정성).
    assert [c.canonical_key for c in gleif] == [
        c.canonical_key for c in GleifSource(s).discover(seg)
    ]


# (소스, 적용 세그먼트, 키 없는 설정 override, 키 있는 설정 override)
_LIVE_CASES = [
    (DartSource, Segment(country="KR", industry="건설"), {}, {"dart_api_key": "k"}),
    (EdgarSource, Segment(country="US", industry="건설"), {}, {"edgar_user_agent": "ua"}),
    (
        CompaniesHouseSource,
        Segment(country="영국", industry="건설"),
        {},
        {"companies_house_api_key": "k"},
    ),
    (
        OpenCorporatesSource,
        Segment(country="KR", industry="건설"),
        {},
        {"opencorporates_api_key": "k"},
    ),
    (
        SearchSource,
        Segment(country="KR", industry="건설"),
        {},
        {"google_cse_key": "k", "google_cse_cx": "cx"},
    ),
]


def test_companies_house_applies_to_gb_only() -> None:
    s = _dry_settings()
    gb = Segment(country="영국", industry="건설")
    us = Segment(country="US", industry="건설")
    assert CompaniesHouseSource(s).applies_to(gb)
    assert not CompaniesHouseSource(s).applies_to(us)


def test_new_placeholder_sources_dry_run_registry_keyed() -> None:
    s = _dry_settings()
    gb = Segment(country="영국", industry="건설")
    kr = Segment(country="KR", industry="건설")
    ch = CompaniesHouseSource(s).discover(gb)
    oc = OpenCorporatesSource(s).discover(kr)
    assert ch and all(c.canonical_key.startswith("reg:companies_house:") for c in ch)
    assert oc and all(c.canonical_key.startswith("reg:opencorporates:") for c in oc)
    # 결정적이어야 한다(제약 ① 안정성).
    assert [c.canonical_key for c in ch] == [
        c.canonical_key for c in CompaniesHouseSource(s).discover(gb)
    ]
    assert [c.canonical_key for c in oc] == [
        c.canonical_key for c in OpenCorporatesSource(s).discover(kr)
    ]


def test_discover_segment_gb_routes_companies_house() -> None:
    # 광범위 업종(미매핑 '전체')으로 GB 전체 소스 라우팅 검증(집계원 포함).
    rows = discover_segment(Segment(country="영국", industry="전체"), _dry_settings())
    sources = {r.source for r in rows}
    # GB: CompaniesHouse(등록처) + GLEIF/Wikidata/OpenCorporates(집계원) + 검색.
    assert sources == {"companies_house", "gleif", "wikidata", "opencorporates", "search"}
    keys = [r.canonical_key for r in rows]
    assert len(keys) == len(set(keys))  # 병합 후 중복 없음(제약 ①).


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
    assert names == {
        "edgar", "dart", "companies_house", "pse", "set", "sgx", "idx", "bursa",
        "gleif", "wikidata", "opencorporates", "search",
    }


def test_exchange_applies_to_country_routing() -> None:
    s = _dry_settings()
    # 광범위 업종(미매핑 '전체')으로 순수 국가 라우팅만 검증(업종 게이팅과 분리).
    ph = Segment(country="필리핀", industry="전체")
    th = Segment(country="TH", industry="전체")
    kr = Segment(country="KR", industry="전체")
    assert PseSource(s).applies_to(ph) and not PseSource(s).applies_to(th)
    assert SetSource(s).applies_to(th) and not SetSource(s).applies_to(kr)


def test_exchange_dry_run_is_listed_and_registry_keyed() -> None:
    s = _dry_settings()
    pse = PseSource(s).discover(Segment(country="PH", industry="제조"))
    set_ = SetSource(s).discover(Segment(country="태국", industry="에너지"))
    assert pse and all(c.canonical_key.startswith("reg:pse:") for c in pse)
    assert all(c.listed == "listed" for c in pse)  # 거래소 산출은 항상 상장.
    assert set_ and all(c.canonical_key.startswith("reg:set:") for c in set_)
    # 결정적이어야 한다(제약 ① 안정성).
    assert [c.canonical_key for c in pse] == [
        c.canonical_key for c in PseSource(s).discover(Segment(country="PH", industry="제조"))
    ]


def test_new_exchanges_applies_to_country_routing() -> None:
    s = _dry_settings()
    # 광범위 업종(미매핑 '전체')으로 순수 국가 라우팅만 검증(업종 게이팅과 분리).
    sg = Segment(country="싱가포르", industry="전체")
    idn = Segment(country="인도네시아", industry="전체")
    my = Segment(country="말레이시아", industry="전체")
    kr = Segment(country="KR", industry="전체")
    assert SgxSource(s).applies_to(sg) and not SgxSource(s).applies_to(kr)
    assert IdxSource(s).applies_to(idn) and not IdxSource(s).applies_to(kr)
    assert BursaSource(s).applies_to(my) and not BursaSource(s).applies_to(kr)


def test_new_exchanges_dry_run_registry_keyed_and_listed() -> None:
    s = _dry_settings()
    sg = SgxSource(s).discover(Segment(country="SG", industry="금융"))
    idn = IdxSource(s).discover(Segment(country="ID", industry="금융"))
    my = BursaSource(s).discover(Segment(country="MY", industry="금융"))
    assert sg and all(c.canonical_key.startswith("reg:sgx:") for c in sg)
    assert idn and all(c.canonical_key.startswith("reg:idx:") for c in idn)
    assert my and all(c.canonical_key.startswith("reg:bursa:") for c in my)
    assert all(c.listed == "listed" for c in sg + idn + my)
    # 결정적이어야 한다(제약 ① 안정성).
    assert [c.canonical_key for c in sg] == [
        c.canonical_key for c in SgxSource(s).discover(Segment(country="SG", industry="금융"))
    ]


def test_discover_segment_merges_by_domain_equivalence(monkeypatch) -> None:
    # 같은 도메인을 등록처(reg:)와 검색(dom:)이 서로 다른 key 로 잡으면 1건으로 병합(제약①).
    from leadcrawler.sources.base import DiscoveredCompany

    class _RegSrc:
        name = "regsrc"

        def applies_to(self, seg: Segment) -> bool:  # noqa: ARG002
            return True

        def discover(self, seg: Segment) -> list[DiscoveredCompany]:  # noqa: ARG002
            return [DiscoveredCompany(
                canonical_key="reg:dart:001", name="삼성", domain="samsung.com",
                registry="dart", registry_id="001", source="regsrc",
            )]

    class _DomSrc:
        name = "domsrc"

        def applies_to(self, seg: Segment) -> bool:  # noqa: ARG002
            return True

        def discover(self, seg: Segment) -> list[DiscoveredCompany]:  # noqa: ARG002
            # 같은 도메인(www. 접두 변형)이지만 dom: key — 병합돼야 한다.
            return [DiscoveredCompany(
                canonical_key="dom:samsung.com", name="삼성전자", domain="https://www.samsung.com",
                source="domsrc",
            )]

    import leadcrawler.sources.registry as reg

    monkeypatch.setattr(reg, "build_sources", lambda settings: [_RegSrc(), _DomSrc()])
    rows = reg.discover_segment(Segment(country="KR", industry="제조"), _dry_settings())
    # 신뢰도 높은 등록처(첫 등장)만 살아남는다.
    assert len(rows) == 1
    assert rows[0].canonical_key == "reg:dart:001"


def test_discover_segment_ph_routes_pse_aggregators_search() -> None:
    # 광범위 업종(미매핑 '전체')으로 PH 전체 소스 라우팅 검증(거래소·집계원 포함).
    rows = discover_segment(Segment(country="PH", industry="전체"), _dry_settings())
    sources = {r.source for r in rows}
    # PH: PSE(거래소) + GLEIF/Wikidata/OpenCorporates(집계원) + 검색. EDGAR/DART/SET 미적용.
    assert sources == {"pse", "gleif", "wikidata", "opencorporates", "search"}
    keys = [r.canonical_key for r in rows]
    assert len(keys) == len(set(keys))  # 병합 후 중복 없음(제약 ①).
