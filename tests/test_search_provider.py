"""검색 공급자(Serper/CSE) 추상화 테스트 — 주입형 FakeFetcher 로 네트워크 없이 검증.

Settings 는 ``_env_file=None`` 으로 만들어 개발 .env(실 키)가 새지 않게 격리한다.
"""

from __future__ import annotations

from typing import Any

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.search import SearchSource
from leadcrawler.sources.search_provider import (
    CseProvider,
    SerperProvider,
    _lr_to_hl,
    build_search_provider,
)


def _settings(**over: Any) -> Settings:
    return Settings(_env_file=None, dry_run=False, **over)


class FakeFetcher:
    """get_json(CSE) / post_json(Serper) 더블."""

    def __init__(self, *, json=None, post_json=None) -> None:
        self._json = json
        self._post_json = post_json

    def get_json(self, url, *, params=None, headers=None):
        return self._json(url, params or {})

    def post_json(self, url, *, json=None, params=None, headers=None):
        return self._post_json(url, json or {}, headers or {})


class FakeLedger:
    """SupportsCostLedger 최소 더블 — record 누적 + 예산 게이트."""

    def __init__(self, *, over: bool = False) -> None:
        self.records: list[str] = []
        self._over = over

    def record(self, provider: str, units: int = 1):
        self.records.append(provider)

    def is_over_budget(self, month_key: str | None = None) -> bool:
        return self._over


def _serper(organic: list[dict]) -> dict:
    return {"organic": organic}


# --- 팩토리 선택 규칙 -----------------------------------------------------

def test_factory_prefers_serper_in_auto() -> None:
    s = _settings(serper_api_key="k", google_cse_key="k", google_cse_cx="cx")
    assert isinstance(build_search_provider(s), SerperProvider)


def test_factory_cse_when_no_serper() -> None:
    assert isinstance(
        build_search_provider(_settings(google_cse_key="k", google_cse_cx="cx")), CseProvider
    )


def test_factory_none_without_keys() -> None:
    assert build_search_provider(_settings()) is None


def test_factory_forced_cse_ignores_serper() -> None:
    s = _settings(search_provider="cse", serper_api_key="k",
                  google_cse_key="k", google_cse_cx="cx")
    assert isinstance(build_search_provider(s), CseProvider)


def test_factory_none_choice_disables() -> None:
    assert build_search_provider(_settings(search_provider="none", serper_api_key="k")) is None


def test_lr_to_hl_conversion() -> None:
    assert _lr_to_hl("lang_ko") == "ko"
    assert _lr_to_hl("lang_zh-CN") == "zh-cn"
    assert _lr_to_hl("") == ""


# --- Serper 라이브 파싱·현지화·과금 --------------------------------------

def test_serper_parses_localizes_and_records_cost() -> None:
    led = FakeLedger()
    captured: dict = {}

    def _post(url, body, headers):
        captured.update(url=url, body=body, headers=headers)
        return _serper([
            {"link": "https://www.acme.co.kr/ir", "title": "ACME"},
            {"link": "https://news.naver.com/x", "title": "네이버뉴스"},  # blocklist.
            {"link": "https://acme.co.kr/about", "title": "중복"},  # 도메인 중복.
        ])

    src = SearchSource(
        _settings(serper_api_key="k", discovery_max_per_source=10),
        fetcher=FakeFetcher(post_json=_post),
        cost_ledger=led,
    )
    out = src.discover(Segment(country="KR", industry="건설"))
    assert [c.domain for c in out] == ["acme.co.kr"]  # blocklist+중복 제거.
    assert captured["headers"].get("X-API-KEY") == "k"
    # 현지화: gl=kr + hl=ko(lang_ko 변환) + 현지어 키워드.
    assert captured["body"]["gl"] == "kr" and captured["body"]["hl"] == "ko"
    assert "IR 투자정보" in captured["body"]["q"]
    # 건설 = 3 동의어(construction·engineering and construction·building contractor) →
    # 동의어마다 1 쿼리 = 3 쿼리 = 3 크레딧. 결과 도메인은 합집합 dedup 으로 acme 1건.
    assert led.records == ["serper", "serper", "serper"]


def test_serper_budget_blocked_does_not_send() -> None:
    led = FakeLedger(over=True)

    def _post(url, body, headers):
        raise AssertionError("예산 초과 시 발송하면 안 된다")

    src = SearchSource(
        _settings(serper_api_key="k", cost_budget_enforce=True),
        fetcher=FakeFetcher(post_json=_post),
        cost_ledger=led,
    )
    assert src.discover(Segment(country="KR", industry="건설")) == []
    assert led.records == []  # 차단 → 과금도 없음.


def test_serper_error_returns_empty() -> None:
    def _post(url, body, headers):
        raise RuntimeError("waf")

    src = SearchSource(
        _settings(serper_api_key="k"), fetcher=FakeFetcher(post_json=_post)
    )
    assert src.discover(Segment(country="KR", industry="건설")) == []
