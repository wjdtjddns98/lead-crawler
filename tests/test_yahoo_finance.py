"""Yahoo Finance 티커→웹사이트 — 심볼 매핑·crumb 흐름·401 재발급·404 무오류·429 래치(네트워크 0)."""
from __future__ import annotations

import httpx

from leadcrawler.sources.yahoo_finance import YahooProfile, yahoo_symbol


def test_symbol_mapping() -> None:
    assert yahoo_symbol("7203", "JP") == "7203.T"
    assert yahoo_symbol("7203.T", "JP") == "7203.T"
    assert yahoo_symbol("AAPL", "US") == "AAPL"
    assert yahoo_symbol("005930", "KR", "KOSPI") == "005930.KS"
    assert yahoo_symbol("035420", "KR", "KOSDAQ") == "035420.KQ"
    assert yahoo_symbol("KR7005930003", "KR", "KOSPI") == "005930.KS"  # fsc ISIN
    assert yahoo_symbol("BRK.A", "US") == "BRK-A"
    assert yahoo_symbol("VNM", "VN") == "VNM.VN"
    assert yahoo_symbol("600519", "CN") == "600519.SS"
    assert yahoo_symbol("000001", "CN") == "000001.SZ"
    assert yahoo_symbol("5", "HK") == "0005.HK"
    assert yahoo_symbol("X", "ZZ") is None  # 미지원 국가
    assert yahoo_symbol("", "JP") is None


def _profile(handler) -> YahooProfile:  # noqa: ANN001
    return YahooProfile(min_interval=0.0, transport=httpx.MockTransport(handler))


def _summary(website: str | None) -> dict:
    return {"quoteSummary": {"result": [{"assetProfile": {"website": website}}], "error": None}}


def test_crumb_then_website() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host + req.url.path)
        if req.url.host == "fc.yahoo.com":
            return httpx.Response(404, headers={"set-cookie": "A1=abc; Path=/"})
        if req.url.path.endswith("/getcrumb"):
            return httpx.Response(200, text="crumb123")
        assert req.url.params["crumb"] == "crumb123"
        return httpx.Response(200, json=_summary("https://www.pharmafoods.co.jp/ir/"))

    p = _profile(handler)
    assert p.website("2929", "JP") == "pharmafoods.co.jp"
    assert p.website("2930", "JP") == "pharmafoods.co.jp"  # crumb 재사용(쿠키·crumb 각 1회)
    assert seen.count("fc.yahoo.com/") == 1 and sum(s.endswith("/getcrumb") for s in seen) == 1


def test_401_refreshes_crumb_once_and_404_is_not_an_error() -> None:
    crumbs = iter(["old", "new"])
    calls = {"summary": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        if req.url.path.endswith("/getcrumb"):
            return httpx.Response(200, text=next(crumbs))
        calls["summary"] += 1
        if req.url.params["crumb"] == "old":
            return httpx.Response(401)
        if "9999" in req.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json=_summary("example.com"))

    p = _profile(handler)
    assert p.website("1234", "JP") == "example.com"
    assert calls["summary"] == 2  # 401 → 재발급 → 재시도
    assert p.website("9999", "JP") is None
    assert p._errors == 0  # 404 는 오류 카운트 안 함


def test_result_null_body_returns_none() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        if req.url.path.endswith("/getcrumb"):
            return httpx.Response(200, text="c")
        return httpx.Response(200, json={"quoteSummary": {"result": None, "error": {"code": "Not Found"}}})

    p = _profile(handler)
    assert p.website("1234", "JP") is None and p._errors == 0


def test_429_latches_after_repeated_errors() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        if req.url.path.endswith("/getcrumb"):
            return httpx.Response(200, text="c")
        calls["n"] += 1
        return httpx.Response(429)

    p = _profile(handler)
    for _ in range(8):
        assert p.website("1234", "JP") is None
    assert p._latched and calls["n"] == 5  # 5회째 래치 → 이후 호출은 네트워크 0
