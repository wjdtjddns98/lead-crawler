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
from leadcrawler.sources.registry import build_sources, close_sources, discover_segment
from leadcrawler.sources.search import SearchSource
from leadcrawler.sources.wikidata import WikidataSource


def _dry_settings(**over: object) -> Settings:
    """dry_run 기본 설정(필요 시 필드 override)."""
    return Settings(dry_run=True, **over)


class _SpyFetcher:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _SpySource:
    """발견 소스 스파이 — discover 호출수 카운트 + 닫을 수 있는 _fetcher 보유(P3 검증용)."""

    def __init__(self) -> None:
        self.name = "spy"
        self.calls = 0
        self.fetcher = _SpyFetcher()
        self._fetcher = self.fetcher  # close_sources 가 찾는 비공개 속성명.

    def applies_to(self, segment: Segment) -> bool:  # noqa: ARG002
        return True

    def discover(self, segment: Segment) -> list:  # noqa: ARG002
        self.calls += 1
        return []


def test_discover_segment_reuses_passed_sources() -> None:
    """sources 를 넘기면 build_sources 로 재생성하지 않고 그 인스턴스를 재사용한다(P3)."""
    spy = _SpySource()
    discover_segment(Segment(country="KR", industry="건설"), _dry_settings(), sources=[spy])
    discover_segment(Segment(country="US", industry="금융"), _dry_settings(), sources=[spy])
    # 같은 인스턴스가 두 세그먼트에 재사용됐다(재빌드면 이 spy 는 호출조차 안 됨).
    assert spy.calls == 2


def test_close_sources_closes_fetchers() -> None:
    """close_sources 가 각 소스의 _fetcher.close() 를 best-effort 호출(누수 제거, P3)."""
    spy = _SpySource()
    close_sources([spy])
    assert spy.fetcher.closed is True
    close_sources([object()])  # _fetcher/close 없는 객체도 안전(no-op).


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

    monkeypatch.setattr(
        reg, "build_sources", lambda settings, cost_ledger=None: [_RegSrc(), _DomSrc()]
    )
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


# ── ① 유료 검색 비용 가드: 글로벌 seen 인지 + 페이지 중복률 조기중단 ──────────────


class _FakeProvider:
    """검색 공급자 스텁 — 호출수 카운트 + 페이지별 결과 주입(CSE 다페이지 모사)."""

    page_size = 10
    max_start = 91

    def __init__(self, pages: list[list[dict]]) -> None:
        self.pages = pages
        self.calls = 0

    def fetch_page(self, query: str, *, gl: str, lr: str, start: int) -> list[dict]:  # noqa: ARG002
        self.calls += 1
        idx = (start - 1) // self.page_size
        return self.pages[idx] if idx < len(self.pages) else []


def _page(*roots: str) -> list[dict]:
    """도메인 root 들로 검색결과 페이지(link+title) 생성."""
    return [{"link": f"https://{r}.com", "title": r.capitalize()} for r in roots]


def test_search_live_filters_global_seen_domains() -> None:
    """① seen 에 있는 도메인은 산출에서 빠지고 신규만 반환된다(중복 비적재)."""
    provider = _FakeProvider([_page("acme0", "acme1", "acme2")])
    src = SearchSource(Settings(dry_run=False))
    seg = Segment(country="KR", industry="건설")
    rows = src._live(seg, provider, seen={"acme0.com"})  # acme0 는 글로벌 기지(중복).
    domains = {r.domain for r in rows}
    assert domains == {"acme1.com", "acme2.com"}  # acme0 제외, 신규만.


def test_search_live_early_aborts_on_low_new_ratio() -> None:
    """① 페이지 신규 비율이 search_min_new_ratio 미만이면 다음 페이지를 사지 않는다."""
    # page1: 10개 중 9개가 기지(seen) → 신규비율 0.1 < 0.2 → page2 fetch 안 함.
    p1 = _page(*[f"d{i}" for i in range(10)])
    p2 = _page(*[f"e{i}" for i in range(10)])
    provider = _FakeProvider([p1, p2])
    src = SearchSource(Settings(dry_run=False))
    seg = Segment(country="KR", industry="건설")
    seen = {f"d{i}.com" for i in range(9)}  # d0..d8 기지, d9 만 신규.
    rows = src._live(seg, provider, seen=seen)
    assert provider.calls == 1  # 조기중단 — page2 과금 안 함.
    assert {r.domain for r in rows} == {"d9.com"}


def test_search_live_no_seen_is_backward_compatible() -> None:
    """① seen=None 이면 기존처럼 전량 페이징(조기중단 없음, 회귀 0)."""
    p1 = _page(*[f"d{i}" for i in range(10)])
    p2 = _page(*[f"e{i}" for i in range(10)])
    provider = _FakeProvider([p1, p2])
    src = SearchSource(Settings(dry_run=False))
    seg = Segment(country="KR", industry="건설")
    rows = src._live(seg, provider, seen=None)
    assert provider.calls == 3  # page1·page2·빈page(종료) — 끝까지 페이징.
    assert len(rows) == 20  # 전부 신규로 적재.


# ── ② 글로벌 seen 주입 + 무료-우선 유료검색 스킵 ─────────────────────────────


class _FreeSrc:
    """무료(비검색) 발견 소스 스텁."""

    name = "freesrc"

    def __init__(self, results: list) -> None:
        self._results = results

    def applies_to(self, segment: Segment) -> bool:  # noqa: ARG002
        return True

    def discover(self, segment: Segment) -> list:  # noqa: ARG002
        return list(self._results)


class _SpySearch(SearchSource):
    """SearchSource 스파이 — discover 호출수·주입된 seen 기록(isinstance 게이트 통과용 서브클래스)."""

    def __init__(self, settings: Settings, results: list) -> None:
        super().__init__(settings)
        self.calls = 0
        self.seen_arg: set | None = None
        self._results = results

    def applies_to(self, segment: Segment) -> bool:  # noqa: ARG002
        return True

    def discover(self, segment: Segment, *, seen: set | None = None) -> list:  # noqa: ARG002
        self.calls += 1
        self.seen_arg = seen
        return list(self._results)


def test_discover_segment_injects_global_and_segment_seen_to_search() -> None:
    """② 검색 소스에 (글로벌 ∪ 이번 세그먼트 무료소스 도메인)을 seen 으로 주입한다."""
    from leadcrawler.sources.base import DiscoveredCompany

    free = _FreeSrc([DiscoveredCompany(
        canonical_key="reg:dart:001", name="삼성", domain="samsung.com", source="freesrc",
    )])
    spy = _SpySearch(_dry_settings(), results=[DiscoveredCompany(
        canonical_key="dom:new.com", name="뉴", domain="new.com", source="search",
    )])
    rows = discover_segment(
        Segment(country="KR", industry="제조"), _dry_settings(),
        sources=[free, spy], seen_domains={"db-seed.com"},
    )
    # 글로벌(db-seed) + 세그먼트 내 무료소스(samsung) 도메인이 검색에 주입됐다.
    assert spy.seen_arg == {"db-seed.com", "samsung.com"}
    assert spy.calls == 1
    assert {r.domain for r in rows} == {"samsung.com", "new.com"}


def test_discover_segment_skips_paid_search_when_free_covers() -> None:
    """② 무료 소스가 search_skip_if_free_ge 이상 신규를 찾으면 유료 검색 호출 자체를 스킵."""
    from leadcrawler.sources.base import DiscoveredCompany

    free = _FreeSrc([DiscoveredCompany(
        canonical_key="reg:dart:001", name="삼성", domain="samsung.com", source="freesrc",
    )])
    spy = _SpySearch(_dry_settings(search_skip_if_free_ge=1), results=[])
    rows = discover_segment(
        Segment(country="KR", industry="제조"),
        _dry_settings(search_skip_if_free_ge=1),
        sources=[free, spy],
    )
    assert spy.calls == 0  # 무료가 1건 커버 → 유료 검색 미호출(비용 절감).
    assert {r.source for r in rows} == {"freesrc"}


def test_discover_segment_skip_disabled_by_default() -> None:
    """② search_skip_if_free_ge=0(기본)이면 무료가 찾아도 유료 검색은 정상 호출(하위호환)."""
    from leadcrawler.sources.base import DiscoveredCompany

    free = _FreeSrc([DiscoveredCompany(
        canonical_key="reg:dart:001", name="삼성", domain="samsung.com", source="freesrc",
    )])
    spy = _SpySearch(_dry_settings(), results=[])  # 기본 skip_ge=0.
    discover_segment(
        Segment(country="KR", industry="제조"), _dry_settings(), sources=[free, spy],
    )
    assert spy.calls == 1  # 스킵 비활성 → 검색 호출됨.
