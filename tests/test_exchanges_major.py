"""AU/IN/CA/HK/TW 거래소 소스(exchanges_major) 테스트 — 네트워크 0(FakeFetcher 주입)."""

from __future__ import annotations

import io
from typing import Any

import openpyxl

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.exchanges_major import (
    AsxSource,
    HkexSource,
    NseSource,
    TsxSource,
    TwseSource,
    _hkex_market,
    _nse_issuer_id,
    _parse_asx_csv,
    _parse_nse_csv,
    _tsx_is_company,
    _tsx_yahoo_ticker,
)
from leadcrawler.sources.yahoo_finance import yahoo_symbol


class FakeFetcher:
    """SupportsFetch 더블 — url 로 응답을 라우팅한다(tests/test_sources_live.py 관례)."""

    def __init__(self, *, json=None, data=None, text=None) -> None:
        self._json = json
        self._data = data
        self._text = text

    def get_json(self, url: str, *, params=None, headers=None) -> Any:
        return self._json(url, params or {})

    def get_bytes(self, url: str, *, params=None, headers=None) -> bytes:
        return self._data(url, params or {})

    def get_text(self, url: str, *, params=None, headers=None) -> str:
        return self._text(url, params or {})


_LISTED = Segment(country="AU", industry="반도체·디스플레이", listed="listed")


def test_dry_run_is_deterministic_and_listed() -> None:
    settings = Settings(dry_run=True)
    for cls, cc in ((AsxSource, "AU"), (NseSource, "IN"), (TsxSource, "CA"),
                    (HkexSource, "HK"), (TwseSource, "TW")):
        seg = Segment(country=cc, industry="건설", listed="listed")
        a, b = cls(settings).discover(seg), cls(settings).discover(seg)
        assert [d.canonical_key for d in a] == [d.canonical_key for d in b]
        assert all(d.canonical_key.startswith(f"reg:{cls.registry}:") for d in a)
        assert all(d.listed == "listed" for d in a)


def test_applies_only_to_own_country_listed_segment() -> None:
    settings = Settings(dry_run=True)
    assert AsxSource(settings).applies_to(Segment(country="호주", industry="건설", listed="listed"))
    assert not AsxSource(settings).applies_to(Segment(country="IN", industry="건설", listed="listed"))
    assert NseSource(settings).applies_to(Segment(country="india", industry="", listed="listed"))
    assert TsxSource(settings).applies_to(Segment(country="canada", industry="은행", listed="listed"))
    assert HkexSource(settings).applies_to(Segment(country="홍콩", industry="은행", listed="listed"))
    assert TwseSource(settings).applies_to(Segment(country="台灣", industry="은행", listed="listed"))


# --- ASX ---

_ASX_CSV = (
    "ASX listed companies as at Fri Oct 23 13:18:54 AEDT 2026\n\n"
    "Company name,ASX code,GICS industry group\n"
    '"1414 DEGREES LIMITED","14D","Capital Goods"\n'
    '"BHP GROUP LIMITED","BHP","Materials"\n'
    '"BHP GROUP LIMITED","BHP","Materials"\n'
    '"",  "ZZZ","Banks"\n'
)


def test_parse_asx_csv_skips_preamble_and_blank_rows() -> None:
    rows = _parse_asx_csv(_ASX_CSV)
    assert [(r["code"], r["name"]) for r in rows] == [
        ("14D", "1414 DEGREES LIMITED"), ("BHP", "BHP GROUP LIMITED"), ("BHP", "BHP GROUP LIMITED"),
    ]
    assert _parse_asx_csv("garbage\nno header") == []
    assert [r["code"] for r in _parse_asx_csv("\ufeff" + _ASX_CSV)][:1] == ["14D"]  # BOM 허용.


def test_asx_live_dedups_and_does_not_stamp_segment_industry() -> None:
    settings = Settings(dry_run=False, exchange_max_per_source=10)
    out = AsxSource(settings, fetcher=FakeFetcher(text=lambda u, p: _ASX_CSV)).discover(_LISTED)
    assert [d.registry_id for d in out] == ["14D", "BHP"]
    assert out[0].ticker == "14D" and out[0].market == "ASX" and out[0].domain is None
    assert out[0].industry != "반도체·디스플레이"  # 거래소 행은 세그먼트 업종 무도장.
    assert yahoo_symbol(out[1].ticker, "AU") == "BHP.AX"


def test_asx_live_error_returns_empty() -> None:
    def boom(u, p):
        raise RuntimeError("down")

    src = AsxSource(Settings(dry_run=False), fetcher=FakeFetcher(text=boom))
    assert src.discover(_LISTED) == []


# --- NSE ---

_NSE_CSV = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT,"
    " ISIN NUMBER, FACE VALUE\n"
    "20MICRONS,20 Microns Limited,EQ,06-OCT-2008,5,1,INE144J01027,5\n"
    "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
    "SUSPND,Suspended Co,BZ,01-JAN-2000,10,1,INE999999999,10\n"
    "TTSEG,Trade For Trade Co,BE,01-JAN-2000,10,1,INE888888888,10\n"
    "RELIANCE,Dup ISIN row,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
)


def test_parse_nse_csv_filters_series_and_normalizes_header_spaces() -> None:
    rows = _parse_nse_csv(_NSE_CSV)
    assert [r["symbol"] for r in rows] == ["20MICRONS", "RELIANCE", "TTSEG", "RELIANCE"]
    assert _parse_nse_csv("") == [] and _parse_nse_csv("A,B\n1,2\n") == []


def test_nse_live_uses_isin_issuer_as_registry_id_and_symbol_as_ticker() -> None:
    settings = Settings(dry_run=False, exchange_max_per_source=10)
    seg = Segment(country="IN", industry="제약·바이오", listed="listed")
    out = NseSource(settings, fetcher=FakeFetcher(text=lambda u, p: _NSE_CSV)).discover(seg)
    assert [d.registry_id for d in out] == ["INE144J", "INE002A", "INE8888"]
    assert out[1].ticker == "RELIANCE" and yahoo_symbol(out[1].ticker, "IN") == "RELIANCE.NS"
    # 액면분할로 일련번호가 바뀐 ISIN 도 같은 발행사 키 → 재추출 없음(제약①).
    assert _nse_issuer_id("INE467B01029") == _nse_issuer_id("INE467B01037") == "INE467B"
    assert _parse_nse_csv("\ufeff" + _NSE_CSV)[0]["symbol"] == "20MICRONS"  # BOM 허용.


# --- TSX ---

def test_tsx_company_filter_and_yahoo_ticker() -> None:
    assert _tsx_is_company("Shopify Inc.")
    assert _tsx_is_company("Allied Properties Real Estate Investment Trust")
    assert not _tsx_is_company("3iQ Bitcoin ETF")
    assert not _tsx_is_company("3M CDR (CAD Hedged)")
    assert not _tsx_is_company("2028 Investment Grade Bond Trust")
    assert not _tsx_is_company("Brookfield Preferred Shares Series 1")
    assert _tsx_yahoo_ticker("REI.UN") == "REI-UN" and _tsx_yahoo_ticker("SHOP") == "SHOP"
    assert yahoo_symbol(_tsx_yahoo_ticker("BBD.B"), "CA") == "BBD-B.TO"


def test_tsx_live_merges_markets_and_marks_partial_on_one_failure() -> None:
    settings = Settings(dry_run=False, exchange_max_per_source=10)

    def route(url, p):
        if url.endswith("/tsx/^*"):
            return {"length": 3, "results": [
                {"symbol": "SHOP", "name": "Shopify Inc."},
                {"symbol": "XIU", "name": "iShares S&P/TSX 60 Index ETF"},
                {"symbol": "SHOP", "name": "Shopify Inc. (dup)"},
                "garbage",
            ]}
        raise RuntimeError("tsxv down")

    src = TsxSource(settings, fetcher=FakeFetcher(json=route))
    out = src.discover(Segment(country="CA", industry="IT·소프트웨어", listed="listed"))
    assert [(d.registry_id, d.market) for d in out] == [("SHOP", "TSX")]
    assert src._partial is True  # 한 시장 실패 → 재수집 커서 기록 생략.


def test_tsx_live_both_markets_ok() -> None:
    settings = Settings(dry_run=False, exchange_max_per_source=10)

    def route(url, p):
        if url.endswith("/tsx/^*"):
            return {"results": [{"symbol": "SHOP", "name": "Shopify Inc."}]}
        return {"results": [{"symbol": "ABC", "name": "Venture Mining Corp."}]}

    src = TsxSource(settings, fetcher=FakeFetcher(json=route))
    out = src.discover(Segment(country="CA", industry="광업·자원", listed="listed"))
    assert [(d.registry_id, d.market, d.ticker) for d in out] == [
        ("SHOP", "TSX", "SHOP"), ("ABC", "TSXV", "ABC"),
    ]
    assert src._partial is False
    assert yahoo_symbol(out[0].ticker, "CA", out[0].market) == "SHOP.TO"
    assert yahoo_symbol(out[1].ticker, "CA", out[1].market) == "ABC.V"  # 벤처는 .V(리뷰 MED).


def test_tsx_live_schema_drift_marks_partial() -> None:
    settings = Settings(dry_run=False)

    def route(url, p):
        if url.endswith("/tsx/^*"):
            return {"results": [{"symbol": "SHOP", "name": "Shopify Inc."}]}
        return {"error": "maintenance"}  # 200 이지만 results 없음.

    src = TsxSource(settings, fetcher=FakeFetcher(json=route))
    out = src.discover(Segment(country="CA", industry="은행", listed="listed"))
    assert [d.registry_id for d in out] == ["SHOP"] and src._partial is True


# --- HKEX ---

def _hkex_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ListOfSecurities"
    ws.append(["List of Securities"])
    ws.append(["Updated as at 23/10/2026"])
    ws.append(["Stock Code", "Name of Securities", "Category", "Sub-Category", "Board Lot", "ISIN",
               "Expiry Date", "Subject to Stamp Duty", "Trading Currency"])
    ws.append(["00001", "CKH HOLDINGS", "Equity", "Equity Securities (Main Board)", "500",
               "KYG217651051", None, "Y", "HKD"])
    ws.append([9988, "BABA-W", "Equity", "Equity Securities (Main Board)", "100",  # 숫자 셀.
               "KYG017191142", None, "Y", "HKD"])
    ws.append(["08001", "GEM CO", "Equity", "Equity Securities (GEM)", "1,000", "HK0000000001",
               None, "Y", "HKD"])
    ws.append(["80700", "TENCENT-R", "Equity", "Equity Securities (Main Board)", "100",
               "KYG875721634", None, "Y", "RMB"])  # 위안화 카운터(중복) 제외.
    ws.append(["02800", "TRACKER FUND", "Equity", "Exchange Traded Products", "500", "HK2800008867",
               None, "Y", "HKD"])
    ws.append(["10001", "SOME WARRANT", "Derivative Warrants", "Derivative Warrants", "10000",
               None, None, "N", "HKD"])
    ws.append(["00001", "CKH HOLDINGS", "Equity", "Equity Securities (Main Board)", "500",
               "KYG217651051", None, "Y", "HKD"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_hkex_live_keeps_equity_securities_only_and_strips_leading_zeros() -> None:
    settings = Settings(dry_run=False, exchange_max_per_source=10)
    data = _hkex_xlsx()
    out = HkexSource(settings, fetcher=FakeFetcher(data=lambda u, p: data)).discover(
        Segment(country="HK", industry="은행", listed="listed")
    )
    assert [(d.registry_id, d.ticker, d.market) for d in out] == [
        ("00001", "1", "HKEX Main Board"), ("09988", "9988", "HKEX Main Board"),
        ("08001", "8001", "HKEX GEM"),
    ]
    assert yahoo_symbol(out[0].ticker, "HK") == "0001.HK"
    assert yahoo_symbol(out[1].ticker, "HK") == "9988.HK"
    assert _hkex_market("weird") == "HKEX"


def test_hkex_live_bad_bytes_returns_empty() -> None:
    src = HkexSource(Settings(dry_run=False), fetcher=FakeFetcher(data=lambda u, p: b"PK-not-xlsx"))
    out = src.discover(Segment(country="HK", industry="은행", listed="listed"))
    assert out == []


# --- TWSE ---

def test_twse_live_merges_twse_and_tpex_with_direct_domains() -> None:
    settings = Settings(dry_run=False, exchange_max_per_source=10)

    def route(url, p):
        if "twse.com.tw" in url:
            return [
                {"公司代號": "1101", "公司名稱": "臺灣水泥股份有限公司", "英文簡稱": "TCC",
                 "網址": "https://www.tccgroupholdings.com/tw/"},
                {"公司代號": "2330", "公司名稱": "台灣積體電路製造股份有限公司", "英文簡稱": "TSMC",
                 "網址": ""},
                {"公司名稱": "코드 없음"},
            ]
        return [
            {"SecuritiesCompanyCode": "1240", "CompanyName": "茂生農經股份有限公司",
             "WebAddress": "https://www.morn-sun.com.tw/　"},
            {"SecuritiesCompanyCode": "1101", "CompanyName": "중복 코드", "WebAddress": "x.com"},
        ]

    out = TwseSource(settings, fetcher=FakeFetcher(json=route)).discover(
        Segment(country="TW", industry="반도체·디스플레이", listed="listed")
    )
    assert [(d.registry_id, d.market, d.domain) for d in out] == [
        ("1101", "TWSE", "tccgroupholdings.com"), ("2330", "TWSE", None),
        ("1240", "TPEx", "morn-sun.com.tw"),
    ]
    assert out[0].name == "TCC" and out[0].name_eng == "臺灣水泥股份有限公司"  # 영문 표시명 우선(#412).
    assert out[2].name == "茂生農經股份有限公司" and out[2].name_eng is None  # TPEx 는 영문명 없음.
    assert yahoo_symbol(out[1].ticker, "TW", out[1].market) == "2330.TW"
    assert yahoo_symbol(out[2].ticker, "TW", out[2].market) == "1240.TWO"  # 상궤는 .TWO(리뷰 MED).


def test_twse_live_one_endpoint_failure_is_partial() -> None:
    settings = Settings(dry_run=False)

    def route(url, p):
        if "twse.com.tw" in url:
            raise RuntimeError("down")
        return [{"SecuritiesCompanyCode": "1240", "CompanyName": "茂生農經", "WebAddress": None}]

    src = TwseSource(settings, fetcher=FakeFetcher(json=route))
    out = src.discover(Segment(country="TW", industry="농림·수산", listed="listed"))
    assert [d.registry_id for d in out] == ["1240"] and src._partial is True


def test_twse_live_schema_drift_marks_partial() -> None:
    settings = Settings(dry_run=False)

    def route(url, p):
        if "twse.com.tw" in url:
            return {"message": "maintenance"}  # 200 이지만 list 아님.
        return [{"SecuritiesCompanyCode": "1240", "CompanyName": "茂生農經", "WebAddress": None}]

    src = TwseSource(settings, fetcher=FakeFetcher(json=route))
    out = src.discover(Segment(country="TW", industry="농림·수산", listed="listed"))
    assert [d.registry_id for d in out] == ["1240"] and src._partial is True
