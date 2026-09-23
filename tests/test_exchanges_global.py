"""유럽 거래소 소스(PR-b: Xetra/Euronext/BME/SIX) 테스트 — 네트워크 0(FakeFetcher 주입)."""

from __future__ import annotations

import io
import json
from typing import Any

import openpyxl

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.exchanges_global import (
    BmeSource,
    DeutscheBoerseSource,
    EuronextSource,
    SixSource,
    _de_blob_url,
    _parse_de_workbook,
    _parse_euronext_row,
    _six_row_active,
)


class FakeFetcher:
    """SupportsFetch 더블 — url/params 로 응답을 라우팅한다(tests/test_sources_live.py 관례)."""

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


def _dry_settings(**over: object) -> Settings:
    return Settings(dry_run=True, **over)


# --- 게이트 회귀(공통) ------------------------------------------------------

def test_applies_to_country_routing() -> None:
    s = _dry_settings()
    de = Segment(country="독일", industry="전체")
    fr = Segment(country="FR", industry="전체")
    it = Segment(country="이탈리아", industry="전체")
    es = Segment(country="ES", industry="전체")
    ch = Segment(country="스위스", industry="전체")
    kr = Segment(country="KR", industry="전체")
    assert DeutscheBoerseSource(s).applies_to(de) and not DeutscheBoerseSource(s).applies_to(kr)
    assert EuronextSource(s).applies_to(fr) and EuronextSource(s).applies_to(it)
    assert not EuronextSource(s).applies_to(kr)
    assert BmeSource(s).applies_to(es) and not BmeSource(s).applies_to(kr)
    assert SixSource(s).applies_to(ch) and not SixSource(s).applies_to(kr)


def test_applies_to_listed_segment_regardless_of_specific_industry() -> None:
    # 상장 세그먼트에선 구체 업종이어도 켜진다(트랙 S 사각 해소, exchanges.py 와 동일 게이트).
    s = _dry_settings()
    listed = Segment(country="독일", industry="제조", listed="listed")
    assert DeutscheBoerseSource(s).applies_to(listed)
    assert not DeutscheBoerseSource(s).applies_to(
        Segment(country="독일", industry="제조", listed="unlisted")
    )


def test_dry_run_is_listed_and_registry_keyed_and_deterministic() -> None:
    s = _dry_settings()
    for cls, seg in (
        (DeutscheBoerseSource, Segment(country="DE", industry="금융")),
        (EuronextSource, Segment(country="FR", industry="금융")),
        (BmeSource, Segment(country="ES", industry="금융")),
        (SixSource, Segment(country="CH", industry="금융")),
    ):
        out = cls(s).discover(seg)
        assert out and all(c.canonical_key.startswith(f"reg:{cls(s).registry}:") for c in out)
        assert all(c.listed == "listed" and c.listed_verified for c in out)
        assert [c.canonical_key for c in out] == [
            c.canonical_key for c in cls(s).discover(seg)
        ]


def test_build_company_does_not_stamp_segment_industry() -> None:
    # 거래소 행 업종은 세그먼트 라벨이 아니라 미분류(도장 금지, 2026-07-13 사고 재발 방지).
    from leadcrawler.sources.taxonomy import UNCLASSIFIED

    s = _dry_settings()
    out = DeutscheBoerseSource(s).discover(Segment(country="독일", industry="제조"))
    assert all(c.industry == UNCLASSIFIED for c in out)


# --- Xetra(DE) --------------------------------------------------------------

def _de_row(sheet_name: str, isin: str, symbol: str, name: str, country: str = "Germany"):
    return {
        "ISIN": isin, "Trading Symbol": symbol, "Company": name,
        "Sector": "x", "Subsector": "y", "Country": country,
    }


def _de_workbook(rows_by_sheet: dict[str, list[dict]]) -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    headers = ["ISIN", "Trading Symbol", "Company", "Sector", "Subsector", "Country"]
    for sheet_name, rows in rows_by_sheet.items():
        ws = wb.create_sheet(sheet_name)
        if sheet_name == "Scale":
            ws.append(["Number of companies", len(rows)])  # 헤더 앞 안내 행(실측 형태).
        ws.append(headers)
        for row in rows:
            ws.append([row.get(h) for h in headers])
    return wb


def test_de_parse_workbook_reads_three_sheets_and_filters_country() -> None:
    wb = _de_workbook({
        "Prime Standard": [
            _de_row("Prime Standard", "DE0001", "AAA", "Alpha AG"),
            _de_row("Prime Standard", "DE0002", "BBB", "외국상장사", country="France"),
        ],
        "General Standard": [_de_row("General Standard", "DE0003", "CCC", "Charlie SE")],
        # Scale 시트: 헤더가 안내 행 뒤(위치 고정 금지 — "ISIN" 셀 탐색으로 찾는지 검증).
        "Scale": [_de_row("Scale", "DE0004", "DDD", "Delta KG")],
        "Basic Board": [_de_row("Basic Board", "DE0005", "EEE", "제외대상")],
    })
    rows = _parse_de_workbook(wb)
    assert [r["isin"] for r in rows] == ["DE0001", "DE0003", "DE0004"]  # 외국상장·Basic Board 제외.
    assert rows[0]["name"] == "Alpha AG" and rows[0]["sheet"] == "Prime Standard"


def test_de_blob_url_parses_landing_href() -> None:
    landing = (
        '<a href="/resource/blob/99/abc/data/Listed-companies.xlsx">다운로드</a>'
    )
    assert _de_blob_url(landing) == (
        "https://www.cashmarket.deutsche-boerse.com/resource/blob/99/abc/data/"
        "Listed-companies.xlsx"
    )
    assert _de_blob_url("<html>없음</html>") is None


def test_xetra_live_end_to_end_via_fake_fetcher() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    wb = _de_workbook({
        "Prime Standard": [_de_row("Prime Standard", "DE0001", "AAA", "Alpha AG")],
        "General Standard": [],
        "Scale": [],
    })
    buf = io.BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()
    landing = '<a href="/resource/blob/1/a/data/Listed-companies.xlsx">dl</a>'

    fetcher = FakeFetcher(
        text=lambda u, p: landing,
        data=lambda u, p: xlsx_bytes,
    )
    out = DeutscheBoerseSource(settings, fetcher=fetcher).discover(
        Segment(country="독일", industry="금융")
    )
    assert [d.registry_id for d in out] == ["DE0001"]
    assert out[0].registry == "xetra" and out[0].market == "Prime Standard"
    assert out[0].domain is None and out[0].ticker == "AAA"


def test_xetra_live_error_returns_empty() -> None:
    settings = Settings(dry_run=False)

    def _boom(u, p):
        raise RuntimeError("network down")

    out = DeutscheBoerseSource(
        settings, fetcher=FakeFetcher(text=lambda u, p: "", data=_boom)
    ).discover(Segment(country="DE", industry="금융"))
    assert out == []


# --- Euronext ----------------------------------------------------------------

def test_parse_euronext_row_extracts_name_isin_symbol_market() -> None:
    row = [
        "", "<a href='/en/product/equities/FR0001-XPAR'>Soci&eacute;t&eacute; G&eacute;n&eacute;rale</a>",
        "FR0001", "GLE", "<div title='Euronext Paris'>XPAR</div>",
    ]
    parsed = _parse_euronext_row(row)
    assert parsed == {
        "isin": "FR0001", "name": "Société Générale", "symbol": "GLE",
        "market": "Euronext Paris",
    }
    assert _parse_euronext_row(["", "no anchor", "", "SYM"]) is None  # ISIN 없음 → None.
    assert _parse_euronext_row("boom") is None


def test_euronext_live_paginates_and_dedups_by_isin() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    page1 = {
        "iTotalRecords": 3,
        "aaData": [
            ["", "<a>Alpha</a>", "FR0001", "ALP", "<div title='Euronext Paris'>XPAR</div>"],
            ["", "<a>Beta</a>", "FR0002", "BET", "<div title='Euronext Paris'>XPAR</div>"],
        ],
    }
    page2 = {
        "iTotalRecords": 3,
        "aaData": [
            ["", "<a>Alpha</a>", "FR0001", "ALP", "<div title='Euronext Paris'>XPAR</div>"],
            ["", "<a>Gamma</a>", "FR0003", "GAM", "<div title='Euronext Paris'>XPAR</div>"],
        ],
    }

    def _json(url: str, params: dict) -> Any:
        return {"0": page1, "2": page2}.get(str(params.get("iDisplayStart")), {"aaData": []})

    out = EuronextSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="프랑스", industry="금융")
    )
    assert [d.registry_id for d in out] == ["FR0001", "FR0002", "FR0003"]  # 중복 ISIN dedup.
    assert all(d.registry == "euronext" and d.market == "Euronext Paris" for d in out)


def test_euronext_live_stops_on_repeated_page() -> None:
    # 같은 페이지(신규 ISIN 0건)를 계속 돌려줘도 무한루프하지 않고 2회 이하로 끝난다.
    settings = Settings(dry_run=False, discovery_max_per_source=100)
    calls = {"n": 0}
    same_page = {
        "iTotalRecords": 10_000,  # 총량은 커도 서버가 같은 페이지를 반복(서버측 이상 시뮬레이션).
        "aaData": [["", "<a>Alpha</a>", "FR0001", "ALP", "<div title='X'>XPAR</div>"]],
    }

    def _json(url: str, params: dict) -> Any:
        calls["n"] += 1
        return same_page

    out = EuronextSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="프랑스", industry="금융")
    )
    assert [d.registry_id for d in out] == ["FR0001"]
    assert calls["n"] <= 2


def test_euronext_unmapped_country_returns_empty_without_call() -> None:
    # applies_to 를 우회해 직접 discover 해도(방어적) MIC 매핑 없는 국가는 네트워크 호출 없이 빈 결과.
    settings = Settings(dry_run=False)

    def _boom(*a, **k):
        raise AssertionError("MIC 매핑 없는 국가는 호출하면 안 된다")

    out = EuronextSource(settings, fetcher=FakeFetcher(json=_boom))._live(
        Segment(country="KR", industry="금융")
    )
    assert out == []


# --- BME -----------------------------------------------------------------

def test_bme_live_two_passes_and_excludes_non_company_segments() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    sibe_page = {
        "hasMoreResults": False,
        "data": [
            {"companyKey": "SAN", "name": "Banco Santander", "tradingSystem": "SIBE",
             "website": "https://www.santander.com"},
            {"companyKey": "SIC1", "name": "SICAV 제외대상", "mtfSegment": "SICAV"},
            {"companyKey": "ETF1", "name": "ETF 제외대상", "tradingSystem": "ETF"},
        ],
    }
    growth_page = {
        "hasMoreResults": False,
        "data": [{"companyKey": "GRW", "name": "Growth Co", "mtfSegment": "BMEGrowth"}],
    }

    def _json(url: str, params: dict) -> Any:
        if params.get("mtfSegment") == "BMEGrowth":
            return growth_page
        return sibe_page

    out = BmeSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="스페인", industry="금융")
    )
    assert [d.registry_id for d in out] == ["SAN", "GRW"]  # SICAV/ETF 제외.
    assert out[0].market == "SIBE" and out[0].domain == "santander.com"
    assert out[1].market == "BME Growth" and out[1].domain is None
    assert all(d.ticker is None for d in out)  # shareName 은 약칭이라 ticker 미사용.


def test_bme_live_stops_when_has_more_results_false() -> None:
    settings = Settings(dry_run=False)
    calls = {"n": 0}

    def _json(url: str, params: dict) -> Any:
        calls["n"] += 1
        return {"hasMoreResults": False, "data": [
            {"companyKey": f"C{calls['n']}", "name": f"Co {calls['n']}"}
        ]}

    BmeSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="ES", industry="금융")
    )
    assert calls["n"] == 2  # SIBE 1콜 + BMEGrowth 1콜(각 첫 페이지에서 종료).


# --- SIX -----------------------------------------------------------------

def test_bme_fetch_pass_stops_on_repeated_page() -> None:
    # hasMoreResults=True 를 계속 줘도 같은 companyKey 만 반복되면 한 패스 안에서 2회 이하로 끝난다.
    settings = Settings(dry_run=False, discovery_max_per_source=100)
    calls = {"n": 0}
    same_page = {"hasMoreResults": True, "data": [{"companyKey": "SAN", "name": "Santander"}]}

    def _json(url: str, params: dict) -> Any:
        calls["n"] += 1
        return same_page

    src = BmeSource(settings, fetcher=FakeFetcher(json=_json))
    out: list = []
    seen: set = set()
    src._fetch_pass(
        src._client(), {"tradingSystem": "SIBE"}, "SIBE", 100,
        src._seg(Segment(country="ES", industry="금융")), out, seen,
    )
    assert [d.registry_id for d in out] == ["SAN"]
    assert calls["n"] <= 2


def test_six_row_active_filters_country_primary_and_delisting() -> None:
    today = 20260923
    assert _six_row_active(
        {"country": "CH", "primaryListing": True, "lastListingDate": 99991231}, today
    )
    assert not _six_row_active(  # 스위스 아님.
        {"country": "DE", "primaryListing": True, "lastListingDate": 99991231}, today
    )
    assert not _six_row_active(  # 2차 상장.
        {"country": "CH", "primaryListing": False, "lastListingDate": 99991231}, today
    )
    assert not _six_row_active(  # 이미 상장폐지(과거일).
        {"country": "CH", "primaryListing": True, "lastListingDate": 20200101}, today
    )
    assert not _six_row_active("boom", today)


def test_six_live_parses_json_from_text_endpoint() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    payload = {
        "status": "Ok",
        "itemList": [
            {"company": "Nestle SA", "isin": "CH0038863350", "valorSymbol": "NESN",
             "valorNumber": "3886335", "country": "CH", "primaryListing": True,
             "lastListingDate": 99991231},
            {"company": "중복 밸러", "valorNumber": "3886335", "country": "CH",
             "primaryListing": True, "lastListingDate": 99991231},
            {"company": "외국사", "valorNumber": "999", "country": "DE",
             "primaryListing": True, "lastListingDate": 99991231},
        ],
    }
    fetcher = FakeFetcher(text=lambda u, p: json.dumps(payload))
    out = SixSource(settings, fetcher=fetcher).discover(Segment(country="스위스", industry="금융"))
    assert [d.registry_id for d in out] == ["3886335"]  # 중복 valorNumber dedup, 외국사 제외.
    assert out[0].registry == "six" and out[0].ticker == "NESN"
    assert out[0].market == "SIX Swiss Exchange" and out[0].domain is None


def test_six_live_malformed_json_returns_empty() -> None:
    settings = Settings(dry_run=False)
    out = SixSource(settings, fetcher=FakeFetcher(text=lambda u, p: "not json")).discover(
        Segment(country="CH", industry="금융")
    )
    assert out == []
