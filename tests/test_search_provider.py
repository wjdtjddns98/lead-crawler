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


# --- Serper 크레딧 소진 latch ---------------------------------------------

class _HttpError(RuntimeError):
    """httpx.HTTPStatusError 더블 — .response(status_code·text)만 흉내."""

    def __init__(self, status: int, text: str = "") -> None:
        super().__init__(f"HTTP {status}")
        from types import SimpleNamespace

        self.response = SimpleNamespace(status_code=status, text=text)


def _latch_provider(exc: Exception) -> tuple[SerperProvider, list[int]]:
    calls: list[int] = []

    def _post(url, body, headers):
        calls.append(1)
        raise exc

    return SerperProvider(_settings(serper_api_key="k"), fetcher=FakeFetcher(post_json=_post)), calls


def test_serper_credits_exhausted_400_latches() -> None:
    """400 "Not enough credits"(라이브 실측 응답) 첫 감지 후 재발송하지 않는다."""
    p, calls = _latch_provider(_HttpError(400, '{"message":"Not enough credits","statusCode":400}'))
    assert p.fetch_page("q", gl="", lr="", start=1) == []
    assert p.fetch_page("q", gl="", lr="", start=1) == []
    assert len(calls) == 1  # 두 번째 호출은 latch 로 발송 자체가 없다.


def test_serper_402_latches() -> None:
    p, calls = _latch_provider(_HttpError(402))
    p.fetch_page("q", gl="", lr="", start=1)
    p.fetch_page("q", gl="", lr="", start=1)
    assert len(calls) == 1


def test_serper_generic_error_does_not_latch() -> None:
    """일반 400(잘못된 요청)·네트워크 오류는 일시 장애 — 영구 차단하면 안 된다."""
    p, calls = _latch_provider(_HttpError(400, '{"message":"query too long"}'))
    p.fetch_page("q", gl="", lr="", start=1)
    p.fetch_page("q", gl="", lr="", start=1)
    assert len(calls) == 2  # latch 없음 — 매 호출 발송.
    p2, calls2 = _latch_provider(RuntimeError("conn reset"))  # response 없는 예외.
    p2.fetch_page("q", gl="", lr="", start=1)
    p2.fetch_page("q", gl="", lr="", start=1)
    assert len(calls2) == 2


# --- Naver 다중앱 로테이션 -------------------------------------------------

class NaverFakeFetcher:
    """get_json(Naver) 호출마다 헤더를 기록하는 더블."""

    def __init__(self) -> None:
        self.headers: list[dict] = []

    def get_json(self, url, *, params=None, headers=None):
        self.headers.append(dict(headers or {}))
        return {"items": [{"link": "https://acme.co.kr", "title": "ACME"}]}


def test_build_naver_provider_none_without_any_pair() -> None:
    from leadcrawler.sources.search_provider import build_naver_provider

    assert build_naver_provider(_settings()) is None
    # 반쪽만 있는 슬롯은 그 슬롯을 카운트하지 않는다.
    assert build_naver_provider(_settings(naver_client_id_2="only-id")) is None


def test_build_naver_provider_single_pair() -> None:
    from leadcrawler.sources.search_provider import NaverProvider, build_naver_provider

    p = build_naver_provider(_settings(naver_client_id="a", naver_client_secret="b"))
    assert isinstance(p, NaverProvider)


def test_naver_rotates_across_configured_apps() -> None:
    """id/secret 3쌍이면 fetch_page 호출마다 라운드로빈으로 자격증명을 순환한다."""
    from leadcrawler.sources.search_provider import build_naver_provider

    f = NaverFakeFetcher()
    p = build_naver_provider(
        _settings(
            naver_client_id="id1", naver_client_secret="sec1",
            naver_client_id_2="id2", naver_client_secret_2="sec2",
            naver_client_id_3="id3", naver_client_secret_3="sec3",
        ),
        fetcher=f,
    )
    for _ in range(4):
        p.fetch_page("q", gl="", lr="", start=1)
    used = [h["X-Naver-Client-Id"] for h in f.headers]
    assert used == ["id1", "id2", "id3", "id1"]  # 3앱 순환, 4번째는 다시 1번.


def test_naver_rotation_thread_safe_even_distribution() -> None:
    """여러 스레드가 동시에 fetch_page 를 불러도 인덱스 경합 없이 정확히 순환한다."""
    import threading

    from leadcrawler.sources.search_provider import build_naver_provider

    f = NaverFakeFetcher()
    p = build_naver_provider(
        _settings(
            naver_client_id="id1", naver_client_secret="sec1",
            naver_client_id_2="id2", naver_client_secret_2="sec2",
        ),
        fetcher=f,
    )
    n_calls = 60

    def _call():
        p.fetch_page("q", gl="", lr="", start=1)

    threads = [threading.Thread(target=_call) for _ in range(n_calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    used = [h["X-Naver-Client-Id"] for h in f.headers]
    assert len(used) == n_calls
    # 인덱스 경합(중복 소비·건너뜀)이 있었다면 두 앱의 호출수가 30/30으로 안 맞는다.
    assert used.count("id1") == n_calls // 2 and used.count("id2") == n_calls // 2


def test_naver_sanitizes_title_tags_and_entities() -> None:
    """네이버 title/description 의 <b> 하이라이트·HTML 엔티티를 공급자에서 정화한다.

    안 하면 발견 회사명에 '칸젠 … <b>화장품</b> &gt; 뉴스' 같은 원문이 그대로 적재되고
    (2026-07-13 라이브 실측 57건), 도메인해석 토큰매칭에도 엔티티가 유입된다.
    """
    from leadcrawler.sources.search_provider import build_naver_provider

    class TagFetcher:
        def get_json(self, url, *, params=None, headers=None):
            return {"items": [{
                "link": "https://acme.co.kr",
                "title": "에이스<b>전자</b> &amp; 부품 &gt; 소개",
                "description": "국내 1위 <b>전자부품</b> 제조사 &quot;에이스&quot;",
            }]}

    p = build_naver_provider(
        _settings(naver_client_id="a", naver_client_secret="b"), fetcher=TagFetcher()
    )
    [item] = p.fetch_page("에이스전자", gl="", lr="", start=1)
    assert item["title"] == "에이스전자 & 부품 > 소개"
    assert item["description"] == '국내 1위 전자부품 제조사 "에이스"'
