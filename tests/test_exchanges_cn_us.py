"""CN(SSE·SZSE)/US(Nasdaq 스크리너) 거래소 소스 테스트 — 네트워크 0(FakeFetcher 주입)."""

from __future__ import annotations

import io
from typing import Any

import openpyxl

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.exchanges_cn_us import (
    NasdaqScreenerSource,
    SseSource,
    SzseSource,
    _nasdaq_company_name,
)
from leadcrawler.sources.yahoo_finance import yahoo_symbol


class FakeFetcher:
    def __init__(self, *, json=None, data=None) -> None:
        self._json = json
        self._data = data

    def get_json(self, url: str, *, params=None, headers=None) -> Any:
        return self._json(url, params or {})

    def get_bytes(self, url: str, *, params=None, headers=None) -> bytes:
        return self._data(url, params or {})


_CN = Segment(country="CN", industry="은행", listed="listed")


def test_dry_run_is_deterministic_and_listed() -> None:
    settings = Settings(dry_run=True)
    for cls, cc in ((SseSource, "CN"), (SzseSource, "CN"), (NasdaqScreenerSource, "US")):
        seg = Segment(country=cc, industry="건설", listed="listed")
        a, b = cls(settings).discover(seg), cls(settings).discover(seg)
        assert [d.canonical_key for d in a] == [d.canonical_key for d in b]
        assert all(d.canonical_key.startswith(f"reg:{cls.registry}:") for d in a)
        assert all(d.listed == "listed" for d in a)
    assert SseSource(settings).applies_to(Segment(country="중국", industry="건설", listed="listed"))
    assert not SseSource(settings).applies_to(Segment(country="US", industry="건설", listed="listed"))
    assert NasdaqScreenerSource(settings).applies_to(
        Segment(country="미국", industry="건설", listed="listed")
    )


# --- SSE ---

def _sse_payload(rows: list[dict]) -> dict:
    return {"pageHelp": {"data": rows, "total": len(rows)}}


def test_sse_live_merges_boards_english_name_and_skips_delisted() -> None:
    settings = Settings(dry_run=False, exchange_max_per_source=10)

    def route(url, p):
        if "STOCK_TYPE=1" in url:
            return _sse_payload([
                {"A_STOCK_CODE": "600000", "FULL_NAME": "上海浦东发展银行股份有限公司",
                 "FULL_NAME_IN_ENGLISH": "Shanghai Pudong Development Bank Co.,Ltd.",
                 "DELIST_DATE": "-"},
                {"A_STOCK_CODE": "600001", "FULL_NAME": "상폐사", "FULL_NAME_IN_ENGLISH": "Gone",
                 "DELIST_DATE": "20200101"},
                {"A_STOCK_CODE": "600000", "FULL_NAME": "중복", "DELIST_DATE": "-"},
            ])
        return _sse_payload([
            {"A_STOCK_CODE": "688981", "FULL_NAME": "中芯国际集成电路制造有限公司",
             "FULL_NAME_IN_ENGLISH": None, "DELIST_DATE": "-"},
        ])

    src = SseSource(settings, fetcher=FakeFetcher(json=route))
    out = src.discover(_CN)
    assert [(d.registry_id, d.market) for d in out] == [
        ("600000", "SSE Main Board"), ("688981", "SSE STAR Market"),
    ]
    assert out[0].name == "Shanghai Pudong Development Bank Co.,Ltd."
    assert out[0].name_eng == "上海浦东发展银行股份有限公司"  # 영문 표시명 우선(#412).
    assert out[1].name == "中芯国际集成电路制造有限公司" and out[1].name_eng is None
    assert yahoo_symbol(out[0].ticker, "CN") == "600000.SS"
    assert out[0].industry != "은행" and src._partial is False  # 세그먼트 업종 무도장.


def test_sse_live_schema_drift_or_error_marks_partial() -> None:
    settings = Settings(dry_run=False)

    def route(url, p):
        if "STOCK_TYPE=1" in url:
            return {"error": "busy"}  # 200 이지만 pageHelp 없음.
        raise RuntimeError("down")

    src = SseSource(settings, fetcher=FakeFetcher(json=route))
    assert src.discover(_CN) == [] and src._partial is True


# --- SZSE ---

def _szse_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["板块", "公司全称", "英文名称", "注册地址", "A股代码", "A股简称", "A股上市日期",
               "A股总股本", "A股流通股本", "B股代码", "B股 简 称", "所属行业", "公司网址"])
    ws.append(["主板", "平安银行股份有限公司", "Ping An Bank Co., Ltd.", "深圳", "000001", "平安银行",
               "1991-04-03", "1", "1", "", "", "J 金融业", "bank.pingan.com"])
    ws.append(["创业板", "宁德时代新能源科技股份有限公司", "", "宁德", 300750, "宁德时代", "2018-06-11",
               "1", "1", "", "", "C 制造业", "https://www.catl.com/"])
    ws.append(["主板", "B주만", "B Only Co.", "深圳", "", "", "", "0", "0", "200011", "B股", "", None])
    ws.append(["主板", "코드 없음", "", "", "", "", "", "0", "0", "", "", "", ""])
    ws.append(["主板", "평안 중복", "", "", "000001", "", "", "", "", "", "", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_szse_live_parses_website_and_falls_back_to_b_share_code() -> None:
    settings = Settings(dry_run=False, exchange_max_per_source=10)
    data = _szse_xlsx()
    out = SzseSource(settings, fetcher=FakeFetcher(data=lambda u, p: data)).discover(_CN)
    assert [(d.registry_id, d.domain, d.market) for d in out] == [
        ("000001", "pingan.com", "SZSE 主板"), ("300750", "catl.com", "SZSE 创业板"),
        ("200011", None, "SZSE 主板"),
    ]
    assert out[0].name == "Ping An Bank Co., Ltd." and out[0].name_eng == "平安银行股份有限公司"
    assert out[1].name == "宁德时代新能源科技股份有限公司"  # 영문명 없음 → 원어 유지.
    assert yahoo_symbol(out[1].ticker, "CN") == "300750.SZ"


def test_szse_live_bad_bytes_returns_empty() -> None:
    src = SzseSource(Settings(dry_run=False), fetcher=FakeFetcher(data=lambda u, p: b"nope"))
    assert src.discover(_CN) == []


# --- Nasdaq ---

def test_nasdaq_company_name_filters_non_common_and_strips_tails() -> None:
    assert _nasdaq_company_name("Apple Inc. Common Stock") == "Apple Inc."
    assert _nasdaq_company_name("Alphabet Inc. Class A Common Stock") == "Alphabet Inc."
    assert _nasdaq_company_name("Alphabet Inc. Class C Capital Stock") == "Alphabet Inc."
    assert (_nasdaq_company_name("Allstate Corporation (The) Common Stock")
            == "Allstate Corporation")
    assert _nasdaq_company_name("Advance Auto Parts Inc.") == "Advance Auto Parts Inc."
    assert (_nasdaq_company_name("Realty Income Corporation Common Stock")
            == "Realty Income Corporation")
    assert (_nasdaq_company_name("Pennsylvania Real Estate Investment Trust Common Stock")
            == "Pennsylvania Real Estate Investment Trust")
    for bad in (
        "Acme Corp Warrant", "Acme Corp Units", "Acme Corp 5.5% Preferred Stock",
        "Acme Corp Rights", "Acme Corp 5.875% Subordinated Debentures due 2059",
        "Acme Corp Notes due 2029", "Acme Corp Depositary Shares each representing 1/1000th",
        "BlackRock Municipal Income Trust", "Sprott Physical Gold Trust",
        "iShares Core S&P 500 ETF", "Armada Acquisition Corp. III Class A Ordinary Share", "",
    ):
        assert _nasdaq_company_name(bad) is None, bad


def test_nasdaq_live_us_only_dedups_classes_and_labels_exchange() -> None:
    settings = Settings(dry_run=False, exchange_max_per_source=10)

    def route(url, p):
        if "exchange=nasdaq" in url:
            return {"data": {"rows": [
                {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "country": "United States"},
                {"symbol": "GOOGL", "name": "Alphabet Inc. Class A Common Stock",
                 "country": "United States"},
                {"symbol": "GOOG", "name": "Alphabet Inc. Class C Capital Stock",
                 "country": "United States"},
                {"symbol": "BABA", "country": "China",
                 "name": "Alibaba Group Holding Limited American Depositary Shares"},
                {"symbol": "SHOP", "name": "Shopify Inc. Class A Subordinate Voting Shares",
                 "country": "Canada"},
                {"symbol": "AACI", "name": "Armada Acquisition Corp. III Class A Ordinary Share",
                 "country": "United States"},
            ]}}
        if "exchange=nyse" in url:
            return {"data": {"rows": [
                {"symbol": "BRK.B", "name": "Berkshire Hathaway Inc. Class B Common Stock",
                 "country": "United States"},
                {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "country": "United States"},
            ]}}
        return {"data": {"rows": []}}  # amex 0행 → 부분 결과.

    src = NasdaqScreenerSource(settings, fetcher=FakeFetcher(json=route))
    out = src.discover(Segment(country="US", industry="IT·소프트웨어", listed="listed"))
    assert [(d.registry_id, d.name, d.market) for d in out] == [
        ("AAPL", "Apple Inc.", "NASDAQ"), ("GOOGL", "Alphabet Inc.", "NASDAQ"),
        ("BRK.B", "Berkshire Hathaway Inc.", "NYSE"),
    ]
    assert yahoo_symbol(out[2].ticker, "US") == "BRK-B"
    assert src._partial is True
