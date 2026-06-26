"""라이브 발견 파싱 테스트 — 주입형 FakeFetcher 로 네트워크 없이 검증."""

from __future__ import annotations

import io
import zipfile
from typing import Any

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
from leadcrawler.sources.search import SearchSource
from leadcrawler.sources.wikidata import WikidataSource


class FakeFetcher:
    """SupportsFetch 더블 — url/params 로 응답을 라우팅한다."""

    def __init__(self, *, json=None, data=None, text=None, post=None) -> None:
        self._json = json
        self._data = data
        self._text = text
        self._post = post

    def get_json(self, url: str, *, params=None, headers=None) -> Any:
        return self._json(url, params or {})

    def get_bytes(self, url: str, *, params=None, headers=None) -> bytes:
        return self._data(url, params or {})

    def get_text(self, url: str, *, params=None, headers=None) -> str:
        return self._text(url, params or {})

    def post_text(self, url: str, *, data=None, params=None, headers=None) -> str:
        return self._post(url, data or {})


def _corp_zip() -> bytes:
    """corpCode.xml(ZIP) 더미 — 상장사 1 + 비상장 1."""
    xml = (
        "<result>"
        "<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
        "<stock_code>005930</stock_code></list>"
        "<list><corp_code>00999999</corp_code><corp_name>비상장사</corp_name>"
        "<stock_code> </stock_code></list>"
        "</result>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml.encode("utf-8"))
    return buf.getvalue()


# --- DART ---------------------------------------------------------------

def test_dart_live_parses_listed_and_industry() -> None:
    settings = Settings(dry_run=False, dart_api_key="k", discovery_max_per_source=10)

    def _json(url: str, params: dict) -> Any:
        assert params["corp_code"] == "00126380"  # 상장사만 상세 조회.
        return {
            "status": "000", "corp_name": "삼성전자",
            "hm_url": "https://www.samsung.com/sec", "induty_code": "264", "corp_cls": "Y",
        }

    src = DartSource(settings, fetcher=FakeFetcher(json=_json, data=lambda u, p: _corp_zip()))
    out = src.discover(Segment(country="KR", industry="반도체"))
    assert len(out) == 1
    dc = out[0]
    assert dc.registry == "dart" and dc.registry_id == "00126380"
    assert dc.domain == "samsung.com" and dc.listed == "listed"
    assert dc.canonical_key == "reg:dart:00126380"


def test_dart_live_industry_filter_excludes_nonmatch() -> None:
    settings = Settings(dry_run=False, dart_api_key="k")

    def _json(url: str, params: dict) -> Any:
        return {"status": "000", "corp_name": "삼성전자", "induty_code": "412", "corp_cls": "Y"}

    # 업종 "반도체"(KSIC 26x) 인데 induty_code 412 → 제외.
    src = DartSource(settings, fetcher=FakeFetcher(json=_json, data=lambda u, p: _corp_zip()))
    assert src.discover(Segment(country="KR", industry="반도체")) == []


def test_dart_no_key_is_noop() -> None:
    src = DartSource(Settings(dry_run=False, dart_api_key=""))
    assert src.discover(Segment(country="KR", industry="반도체")) == []


# --- EDGAR --------------------------------------------------------------

def test_edgar_live_parses_universe_and_domain() -> None:
    settings = Settings(dry_run=False, edgar_user_agent="LeadCrawler test@example.com")
    tickers = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [320193, "Apple Inc.", "AAPL", "Nasdaq"],
            [789019, "MICROSOFT CORP", "MSFT", "Nasdaq"],
            [111, "OTC Co", "OTCX", "Pinksheets"],  # 거래소 아님 → 제외.
        ],
    }
    subs = {
        320193: {"name": "Apple Inc.", "sic": "3571",
                 "website": "https://apple.com", "investorWebsite": "https://investor.apple.com"},
        789019: {"name": "MICROSOFT CORP", "sic": "7372", "website": "", "investorWebsite": ""},
    }

    def _json(url: str, params: dict) -> Any:
        if "company_tickers_exchange" in url:
            return tickers
        cik = int(url.split("CIK")[1].split(".")[0])
        return subs[cik]

    # 업종 미매핑("기타") → 필터 통과(전량), 거래소 상장 2건만.
    src = EdgarSource(settings, fetcher=FakeFetcher(json=_json))
    out = src.discover(Segment(country="US", industry="기타"))
    assert {d.registry_id for d in out} == {"320193", "789019"}
    apple = next(d for d in out if d.registry_id == "320193")
    assert apple.domain == "apple.com"  # investorWebsite 우선.
    msft = next(d for d in out if d.registry_id == "789019")
    assert msft.domain is None  # website 비면 도메인 None(enrich 단계로).


# --- Search (Google CSE) ------------------------------------------------

def test_search_live_filters_blocklist_and_dedup() -> None:
    settings = Settings(
        dry_run=False, google_cse_key="k", google_cse_cx="cx", discovery_max_per_source=10
    )
    page = {
        "items": [
            {"link": "https://www.acme.co.kr/ir", "title": "ACME"},
            {"link": "https://news.naver.com/x", "title": "네이버뉴스"},  # blocklist.
            {"link": "https://www.acme.co.kr/about", "title": "ACME about"},  # 중복 도메인.
        ]
    }

    def _json(url: str, params: dict) -> Any:
        return page if params.get("start", 1) == 1 else {"items": []}

    src = SearchSource(settings, fetcher=FakeFetcher(json=_json))
    out = src.discover(Segment(country="KR", industry="건설"))
    assert len(out) == 1
    assert out[0].domain == "acme.co.kr"
    assert out[0].canonical_key == "dom:acme.co.kr"


def test_search_bing_only_key_is_noop() -> None:
    # Bing API 폐기 → bing 키만으론 라이브 미동작(no-op).
    src = SearchSource(Settings(dry_run=False, bing_api_key="b"))
    assert src.discover(Segment(country="KR", industry="건설")) == []


# --- 신뢰불가 응답 크래시 벡터(graceful degradation) -------------------

def test_dart_numeric_industry_code_does_not_crash() -> None:
    settings = Settings(dry_run=False, dart_api_key="k")

    def _json(url: str, params: dict) -> Any:
        # induty_code 가 숫자(int)로 와도 크래시 없이 매칭돼야 한다.
        return {"status": "000", "corp_name": "삼성전자", "induty_code": 264, "corp_cls": "Y"}

    src = DartSource(settings, fetcher=FakeFetcher(json=_json, data=lambda u, p: _corp_zip()))
    assert len(src.discover(Segment(country="KR", industry="반도체"))) == 1


def test_dart_broken_zip_returns_empty() -> None:
    settings = Settings(dry_run=False, dart_api_key="k")
    src = DartSource(settings, fetcher=FakeFetcher(data=lambda u, p: b"not-a-zip"))
    assert src.discover(Segment(country="KR", industry="반도체")) == []


def test_dart_non_dict_company_is_skipped() -> None:
    settings = Settings(dry_run=False, dart_api_key="k")
    src = DartSource(
        settings, fetcher=FakeFetcher(json=lambda u, p: "error", data=lambda u, p: _corp_zip())
    )
    assert src.discover(Segment(country="KR", industry="반도체")) == []


def test_edgar_non_dict_universe_returns_empty() -> None:
    settings = Settings(dry_run=False, edgar_user_agent="ua")
    # tickers 응답이 dict 가 아니어도(리스트) 크래시 없이 빈 결과.
    src = EdgarSource(settings, fetcher=FakeFetcher(json=lambda u, p: []))
    assert src.discover(Segment(country="US", industry="기타")) == []


def test_edgar_numeric_sic_and_non_dict_sub() -> None:
    settings = Settings(dry_run=False, edgar_user_agent="ua")
    tickers = {"fields": ["cik", "name", "ticker", "exchange"],
               "data": [[1, "A Corp", "A", "Nasdaq"], [2, "B Corp", "B", "Nasdaq"]]}

    def _json(url: str, params: dict) -> Any:
        if "company_tickers_exchange" in url:
            return tickers
        cik = int(url.split("CIK")[1].split(".")[0])
        if cik == 1:
            return {"name": "A Corp", "sic": 3674, "website": "https://a.com"}  # 숫자 SIC.
        return None  # non-dict → skip.

    src = EdgarSource(settings, fetcher=FakeFetcher(json=_json))
    out = src.discover(Segment(country="US", industry="반도체"))
    assert {d.registry_id for d in out} == {"1"}


def test_search_empty_and_non_dict_items() -> None:
    settings = Settings(dry_run=False, google_cse_key="k", google_cse_cx="cx")
    src = SearchSource(settings, fetcher=FakeFetcher(json=lambda u, p: {"items": [None, 1, "x"]}))
    assert src.discover(Segment(country="KR", industry="건설")) == []


# --- GLEIF (JSON:API) ---------------------------------------------------

def _gleif_page(records: list[dict]) -> dict:
    """GLEIF lei-records JSON:API 응답 더미."""
    return {"data": records}


def test_gleif_live_parses_active_and_filters_inactive() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    page = _gleif_page([
        {"id": "PH0000ACTIVE001", "attributes": {"entity": {
            "legalName": {"name": "Ayala Corporation"}, "status": "ACTIVE",
            "legalAddress": {"country": "PH"}}}},
        {"id": "PH0000INACTIVE9", "attributes": {"entity": {
            "legalName": {"name": "Dead Corp"}, "status": "INACTIVE",
            "legalAddress": {"country": "PH"}}}},  # 비활성 → 제외(제약 ②).
    ])

    def _json(url: str, params: dict):
        # 1페이지엔 데이터, 2페이지부턴 빈 목록(페이징 종료).
        return page if params.get("page[number]", 1) == 1 else {"data": []}

    out = GleifSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="필리핀", industry="제조")
    )
    assert len(out) == 1
    dc = out[0]
    assert dc.name == "Ayala Corporation" and dc.registry == "lei"
    assert dc.registry_id == "PH0000ACTIVE001"
    assert dc.domain is None  # LEI 레코드엔 웹사이트 없음.
    assert dc.canonical_key == "reg:lei:ph0000active001"


def test_gleif_non_dict_payload_returns_empty() -> None:
    settings = Settings(dry_run=False)
    src = GleifSource(settings, fetcher=FakeFetcher(json=lambda u, p: ["not", "a", "dict"]))
    assert src.discover(Segment(country="TH", industry="제조")) == []


def test_gleif_unknown_country_is_noop() -> None:
    # 미등록 국가는 applies_to=False 이지만 discover 도 방어적으로 빈 결과.
    settings = Settings(dry_run=False)
    src = GleifSource(settings, fetcher=FakeFetcher(json=lambda u, p: _gleif_page([])))
    assert src.discover(Segment(country="Atlantis", industry="제조")) == []


# --- Wikidata (SPARQL) --------------------------------------------------

def _wd_results(rows: list[dict]) -> dict:
    """Wikidata SPARQL JSON 결과 더미."""
    return {"results": {"bindings": rows}}


def test_wikidata_live_parses_label_and_website() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    rows = _wd_results([
        {
            "item": {"value": "http://www.wikidata.org/entity/Q1391"},
            "itemLabel": {"value": "PTT Public Company"},
            "website": {"value": "https://www.ptt.com/th"},
        },
        {  # 웹사이트 없는 항목 — 도메인 None 으로 통과.
            "item": {"value": "http://www.wikidata.org/entity/Q9999"},
            "itemLabel": {"value": "Siam Cement"},
        },
        {  # 라벨 미해소(QID 가 라벨) → 스킵.
            "item": {"value": "http://www.wikidata.org/entity/Q12345"},
            "itemLabel": {"value": "Q12345"},
        },
    ])
    src = WikidataSource(settings, fetcher=FakeFetcher(json=lambda u, p: rows))
    out = src.discover(Segment(country="태국", industry="에너지"))
    assert {d.registry_id for d in out} == {"Q1391", "Q9999"}
    ptt = next(d for d in out if d.registry_id == "Q1391")
    assert ptt.domain == "ptt.com" and ptt.canonical_key == "reg:wikidata:q1391"
    siam = next(d for d in out if d.registry_id == "Q9999")
    assert siam.domain is None


def test_wikidata_non_dict_payload_returns_empty() -> None:
    settings = Settings(dry_run=False)
    src = WikidataSource(settings, fetcher=FakeFetcher(json=lambda u, p: "boom"))
    assert src.discover(Segment(country="TH", industry="제조")) == []


# --- 거래소 상장목록(Tier B) — PSE 는 POST 폼 + HTML(2026-06-19 실연동 검증) ----

def _pse_row(cmpy_id: str, sec_id: str, name: str, symbol: str) -> str:
    """PSE companyDirectory 한 행 HTML(실제 마크업 형태)."""
    a = f"<a href=\"#company\" onclick=\"cmDetail('{cmpy_id}','{sec_id}');return false;\">"
    return (
        f"<tr><td>{a}{name}</a></td>"
        f"<td class=\"alignC\">{a}{symbol}</a></td>"
        f"<td>Holding Firms</td><td>Holding Firms</td><td class=\"alignC\">Mar 22, 1973</td></tr>"
    )


def _pse_page(rows_html: str) -> str:
    return f"<table><tr><th>Company Name</th><th>Stock Symbol</th></tr>{rows_html}</table>"


def test_pse_live_parses_html_and_paginates() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    page1 = _pse_page(
        _pse_row("55", "347", "Asia Amalgamated Holdings Corporation", "AAA")
        + _pse_row("19", "181", "Atok-Big Wedge Co., Inc.", "AB")
        + _pse_row("55", "347", "중복 심볼 회사", "AAA")  # symbol 중복 → 스킵.
    )
    page2 = _pse_page(_pse_row("174", "173", "AbaCore Capital Holdings, Inc.", "ABA"))

    def _post(url: str, data: dict) -> str:
        # pageNo 로 페이지네이션, 3페이지부턴 빈 목록(종료).
        return {"1": page1, "2": page2}.get(str(data.get("pageNo")), _pse_page(""))

    out = PseSource(settings, fetcher=FakeFetcher(post=_post)).discover(
        Segment(country="필리핀", industry="제조")
    )
    assert [d.registry_id for d in out] == ["AAA", "AB", "ABA"]  # 페이지 가로질러 dedup.
    dc = out[0]
    assert dc.registry == "pse" and dc.listed == "listed"
    assert dc.name == "Asia Amalgamated Holdings Corporation"
    assert dc.domain is None  # 목록엔 웹사이트 없음(enrich 단계로).
    assert dc.canonical_key == "reg:pse:aaa"


def test_pse_live_respects_cap() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=1)
    page = _pse_page(
        _pse_row("1", "1", "Alpha Corp", "ALP") + _pse_row("2", "2", "Beta Corp", "BET")
    )
    out = PseSource(settings, fetcher=FakeFetcher(post=lambda u, d: page)).discover(
        Segment(country="PH", industry="제조")
    )
    assert len(out) == 1 and out[0].registry_id == "ALP"


def test_pse_live_empty_html_returns_empty() -> None:
    settings = Settings(dry_run=False)
    out = PseSource(settings, fetcher=FakeFetcher(post=lambda u, d: "<table></table>")).discover(
        Segment(country="PH", industry="제조")
    )
    assert out == []


def test_sgx_live_parses_securities() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    payload = {"data": {"prices": [
        {"nc": "D05", "n": "DBS Group Holdings"},
        {"nc": "O39", "n": "OCBC Bank"},
        {"nc": "D05", "n": "중복 코드"},  # 코드 중복 → 스킵.
        {"nc": "", "n": "코드 없음"},  # 코드 없음 → 스킵.
    ]}}
    out = SgxSource(settings, fetcher=FakeFetcher(json=lambda u, p: payload)).discover(
        Segment(country="싱가포르", industry="금융")
    )
    assert [d.registry_id for d in out] == ["D05", "O39"]
    dc = out[0]
    assert dc.registry == "sgx" and dc.listed == "listed"
    assert dc.name == "DBS Group Holdings" and dc.domain is None
    assert dc.canonical_key == "reg:sgx:d05"


def test_sgx_live_error_returns_empty() -> None:
    settings = Settings(dry_run=False)

    def _boom(u, p):
        raise RuntimeError("waf")

    out = SgxSource(settings, fetcher=FakeFetcher(json=_boom)).discover(
        Segment(country="SG", industry="금융")
    )
    assert out == []


def test_idx_live_parses_and_paginates() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    page1 = {"data": [
        {"KodeEmiten": "BBCA", "NamaEmiten": "Bank Central Asia Tbk"},
        {"KodeEmiten": "BBRI", "NamaEmiten": "Bank Rakyat Indonesia Tbk"},
    ]}
    page2 = {"data": [{"KodeEmiten": "TLKM", "NamaEmiten": "Telkom Indonesia Tbk"}]}

    def _json(url: str, params: dict) -> Any:
        return {"0": page1, "2": page2}.get(str(params.get("start")), {"data": []})

    out = IdxSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="인도네시아", industry="금융")
    )
    assert [d.registry_id for d in out] == ["BBCA", "BBRI", "TLKM"]
    assert out[0].registry == "idx" and out[0].listed == "listed"
    assert out[0].canonical_key == "reg:idx:bbca"


def test_idx_non_dict_payload_returns_empty() -> None:
    settings = Settings(dry_run=False)
    out = IdxSource(settings, fetcher=FakeFetcher(json=lambda u, p: "boom")).discover(
        Segment(country="ID", industry="금융")
    )
    assert out == []


def test_idx_dict_data_does_not_spin() -> None:
    # data 가 list 가 아닌 dict(예상밖 스키마)면 무한 페이징 없이 1회만 호출 후 종료.
    settings = Settings(dry_run=False)
    calls = {"n": 0}

    def _json(url: str, params: dict) -> Any:
        calls["n"] += 1
        return {"data": {"unexpected": 1}}

    out = IdxSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="ID", industry="금융")
    )
    assert out == [] and calls["n"] == 1


def test_bursa_live_is_disabled_unverified() -> None:
    # Bursa 라이브는 정적 수집 불가 — 네트워크 호출 없이 빈 결과(검증대기).
    settings = Settings(dry_run=False)

    def _boom(*a, **k):
        raise AssertionError("Bursa 라이브는 네트워크를 호출하면 안 된다(검증대기 no-op)")

    out = BursaSource(settings, fetcher=FakeFetcher(json=_boom, post=_boom)).discover(
        Segment(country="말레이시아", industry="금융")
    )
    assert out == []


def test_set_live_is_disabled_waf_blocked() -> None:
    # SET 라이브는 Incapsula WAF 차단으로 비활성 — 네트워크 호출 없이 빈 결과.
    settings = Settings(dry_run=False)

    def _boom(*a, **k):
        raise AssertionError("SET 라이브는 네트워크를 호출하면 안 된다(WAF 비활성)")

    out = SetSource(settings, fetcher=FakeFetcher(json=_boom, post=_boom)).discover(
        Segment(country="태국", industry="에너지")
    )
    assert out == []


# --- Track A: enable_bypass 시 SET/Bursa 우회 파싱(canned HTML) -----------

_LISTING_HTML = (
    "<table>"
    '<tr><td><a href="/q/PTT">PTT</a></td><td>PTT Public Company Limited</td></tr>'
    '<tr><td><a href="/q/AOT">AOT</a></td><td>Airports of Thailand PCL</td></tr>'
    "</table>"
)


def test_set_bypass_parses_listing() -> None:
    # enable_bypass + 우회 페처가 목록 HTML 을 주면 (심볼,회사명) 파싱.
    settings = Settings(dry_run=False, enable_bypass=True, discovery_max_per_source=10)
    src = SetSource(settings, fetcher=FakeFetcher(text=lambda u, p: _LISTING_HTML))
    out = src.discover(Segment(country="태국", industry="에너지"))
    assert len(out) == 2
    assert out[0].registry == "set" and out[0].registry_id == "PTT"
    assert out[0].name == "PTT Public Company Limited"
    assert out[0].canonical_key == "reg:set:ptt" and out[0].listed == "listed"


def test_bursa_bypass_parses_listing() -> None:
    settings = Settings(dry_run=False, enable_bypass=True, discovery_max_per_source=10)
    src = BursaSource(settings, fetcher=FakeFetcher(text=lambda u, p: _LISTING_HTML))
    out = src.discover(Segment(country="말레이시아", industry="금융"))
    assert {c.registry_id for c in out} == {"PTT", "AOT"}
    assert all(c.registry == "bursa" for c in out)


def test_set_bypass_empty_html_is_graceful() -> None:
    # 우회해도 WAF 가 빈 HTML 을 주면(차단 지속) graceful 빈 결과.
    settings = Settings(dry_run=False, enable_bypass=True)
    out = SetSource(settings, fetcher=FakeFetcher(text=lambda u, p: "")).discover(
        Segment(country="태국", industry="에너지")
    )
    assert out == []


def test_set_bypass_respects_cap() -> None:
    settings = Settings(dry_run=False, enable_bypass=True, discovery_max_per_source=1)
    out = SetSource(settings, fetcher=FakeFetcher(text=lambda u, p: _LISTING_HTML)).discover(
        Segment(country="태국", industry="에너지")
    )
    assert len(out) == 1  # 캡 적용


# --- 검색 현지화(Tier C) ------------------------------------------------

def test_search_localizes_query_and_region_by_country() -> None:
    settings = Settings(
        dry_run=False, google_cse_key="k", google_cse_cx="cx", discovery_max_per_source=10
    )
    captured: dict = {}

    def _json(url: str, params: dict):
        captured.update(params)  # 첫 페이지 파라미터 캡처.
        return {"items": []}

    SearchSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="태국", industry="에너지")
    )
    # 태국 → gl=th, lr=lang_th, 현지어 키워드 포함.
    assert captured.get("gl") == "th" and captured.get("lr") == "lang_th"
    assert "เว็บไซต์ทางการ" in captured.get("q", "")


def test_search_unknown_country_uses_default_locale() -> None:
    settings = Settings(
        dry_run=False, google_cse_key="k", google_cse_cx="cx", discovery_max_per_source=10
    )
    captured: dict = {}

    def _json(url: str, params: dict):
        captured.update(params)
        return {"items": []}

    SearchSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="Atlantis", industry="제조")
    )
    # 미등록 국가 → gl/lr 미설정(generic 영어 쿼리).
    assert "gl" not in captured and "lr" not in captured
    assert "investor relations" in captured.get("q", "")


# --- Companies House (advanced-search) ----------------------------------

def test_companies_house_live_parses_active_and_sic() -> None:
    settings = Settings(
        dry_run=False, companies_house_api_key="k", discovery_max_per_source=10
    )
    page = {
        "items": [
            {"company_number": "01234567", "company_name": "Balfour Construction Ltd",
             "company_status": "active", "sic_codes": ["41201", "43999"]},
            {"company_number": "07654321", "company_name": "Dead Construction Ltd",
             "company_status": "dissolved", "sic_codes": ["41100"]},  # 폐업 → 제외(제약②).
            {"company_number": "09999999", "company_name": "Finance Co",
             "company_status": "active", "sic_codes": ["64999"]},  # 업종 불일치 → 제외.
        ]
    }

    def _json(url: str, params: dict) -> Any:
        # start_index 0 에 데이터, 이후는 빈 목록(페이징 종료).
        return page if params.get("start_index", 0) == 0 else {"items": []}

    out = CompaniesHouseSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="영국", industry="건설")
    )
    assert len(out) == 1
    dc = out[0]
    assert dc.registry == "companies_house" and dc.registry_id == "01234567"
    assert dc.domain is None  # 등록처 레코드엔 웹사이트 없음.
    assert dc.canonical_key == "reg:companies_house:01234567"


def test_companies_house_no_industry_map_keeps_all_active() -> None:
    settings = Settings(dry_run=False, companies_house_api_key="k")
    page = {"items": [
        {"company_number": "1", "company_name": "A Ltd", "company_status": "active",
         "sic_codes": ["99999"]},
    ]}
    out = CompaniesHouseSource(
        settings, fetcher=FakeFetcher(json=lambda u, p: page if p.get("start_index", 0) == 0
                                      else {"items": []})
    ).discover(Segment(country="영국", industry="기타"))  # 미매핑 업종 → 전량.
    assert {d.registry_id for d in out} == {"1"}


def test_companies_house_no_key_is_noop() -> None:
    src = CompaniesHouseSource(Settings(dry_run=False, companies_house_api_key=""))
    assert src.discover(Segment(country="영국", industry="건설")) == []


def test_companies_house_non_dict_payload_returns_empty() -> None:
    settings = Settings(dry_run=False, companies_house_api_key="k")
    src = CompaniesHouseSource(settings, fetcher=FakeFetcher(json=lambda u, p: "boom"))
    assert src.discover(Segment(country="영국", industry="건설")) == []


# --- OpenCorporates (companies/search) ----------------------------------

def _oc_page(companies: list[dict]) -> dict:
    """OpenCorporates companies/search 응답 더미."""
    return {"results": {"companies": companies}}


def test_opencorporates_live_parses_active_and_filters_inactive() -> None:
    settings = Settings(
        dry_run=False, opencorporates_api_key="k", discovery_max_per_source=10
    )
    page = _oc_page([
        {"company": {"name": "Acme Manufacturing", "company_number": "12345",
                     "jurisdiction_code": "kr", "inactive": False}},
        {"company": {"name": "Defunct Co", "company_number": "99999",
                     "jurisdiction_code": "kr", "inactive": True}},  # 폐업 → 제외(제약②).
    ])

    def _json(url: str, params: dict) -> Any:
        return page if params.get("page", 1) == 1 else _oc_page([])

    out = OpenCorporatesSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="KR", industry="제조")
    )
    assert len(out) == 1
    dc = out[0]
    assert dc.registry == "opencorporates" and dc.registry_id == "kr/12345"
    assert dc.domain is None
    assert dc.canonical_key == "reg:opencorporates:kr/12345"


def test_opencorporates_inactive_null_or_missing_passes_through() -> None:
    # inactive 가 null/누락인 관할(상태 미추적)은 통과 — 최종 실존판정은 다운스트림 verify.
    settings = Settings(dry_run=False, opencorporates_api_key="k")
    page = _oc_page([
        {"company": {"name": "Null Status Co", "company_number": "1",
                     "jurisdiction_code": "kr", "inactive": None}},
        {"company": {"name": "Missing Status Co", "company_number": "2",
                     "jurisdiction_code": "kr"}},  # inactive 키 자체 없음.
    ])

    def _json(url: str, params: dict) -> Any:
        return page if params.get("page", 1) == 1 else _oc_page([])

    out = OpenCorporatesSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="KR", industry="제조")
    )
    assert {d.registry_id for d in out} == {"kr/1", "kr/2"}


def test_opencorporates_translates_korean_industry_to_english_q() -> None:
    # 한글 업종은 영어 검색어로 옮겨 라틴 색인 recall 을 확보한다(silent recall 실패 방지).
    settings = Settings(dry_run=False, opencorporates_api_key="k")
    captured: dict = {}

    def _json(url: str, params: dict) -> Any:
        captured.update(params)
        return _oc_page([])

    OpenCorporatesSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="KR", industry="제조")
    )
    assert captured.get("q") == "manufacturing"
    assert captured.get("country_code") == "kr"


def test_opencorporates_no_key_is_noop() -> None:
    src = OpenCorporatesSource(Settings(dry_run=False, opencorporates_api_key=""))
    assert src.discover(Segment(country="KR", industry="제조")) == []


def test_opencorporates_non_dict_payload_returns_empty() -> None:
    settings = Settings(dry_run=False, opencorporates_api_key="k")
    src = OpenCorporatesSource(settings, fetcher=FakeFetcher(json=lambda u, p: ["x"]))
    assert src.discover(Segment(country="KR", industry="제조")) == []


def test_opencorporates_unknown_country_is_noop() -> None:
    settings = Settings(dry_run=False, opencorporates_api_key="k")
    src = OpenCorporatesSource(settings, fetcher=FakeFetcher(json=lambda u, p: _oc_page([])))
    assert src.discover(Segment(country="Atlantis", industry="제조")) == []
