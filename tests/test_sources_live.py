"""라이브 발견 파싱 테스트 — 주입형 FakeFetcher 로 네트워크 없이 검증."""

from __future__ import annotations

import io
import zipfile
from typing import Any

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.dart import DartSource
from leadcrawler.sources.edgar import EdgarSource
from leadcrawler.sources.search import SearchSource


class FakeFetcher:
    """SupportsFetch 더블 — url/params 로 응답을 라우팅한다."""

    def __init__(self, *, json=None, data=None) -> None:
        self._json = json
        self._data = data

    def get_json(self, url: str, *, params=None, headers=None) -> Any:
        return self._json(url, params or {})

    def get_bytes(self, url: str, *, params=None, headers=None) -> bytes:
        return self._data(url, params or {})


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
