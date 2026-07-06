"""AI 디렉토리 발견 소스 테스트 — dry_run 더미·게이팅·라이브 경로·인젝션 안전·과금가드.

네트워크·과금 없이 통과한다: SERP·페이지 fetch 는 페이크 페처로, LLM 은 sys.modules 에
주입한 페이크 anthropic 으로 대체한다(:mod:`test_industry_classify` 패턴).
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from leadcrawler.config import Settings
from leadcrawler.sources.ai_directory import (
    AiDirectorySource,
    ClaudeDirectoryExtractor,
    _english_country_name,
    _parse_companies,
)
from leadcrawler.sources.base import Segment
from leadcrawler.sources.countries import resolve_country


def _s(**over: object) -> Settings:
    """hermetic 설정 — 로컬 .env(라이브 토큰·플래그) 오염 차단."""
    return Settings(_env_file=None, **over)


# ── 페이크 인프라 ────────────────────────────────────────────────────────────
class _FakeFetcher:
    """SERP(post_json) + 페이지(get_text) 겸용 페이크 — 소스에 주입해 provider·페이지 fetch 공용."""

    def __init__(self, organic: list[dict], pages: dict[str, str]) -> None:
        self._organic = organic
        self._pages = pages
        self.fetched: list[str] = []

    def post_json(self, url, *, json=None, params=None, headers=None):  # noqa: A002, ARG002
        return {"organic": self._organic}

    def get_text(self, url, *, params=None, headers=None):  # noqa: ARG002
        self.fetched.append(url)
        return self._pages.get(url, "")


class _Ledger:
    def __init__(self, over: bool = False) -> None:
        self._over = over
        self.records: list[str] = []

    def is_over_budget(self, *_a, **_k) -> bool:
        return self._over

    def record(self, provider: str, units: int = 1):  # noqa: ARG002
        self.records.append(provider)
        return None


class _FakeMsg:
    def __init__(self, text: str) -> None:
        self.content = [types.SimpleNamespace(type="text", text=text)]


class _FakeMessages:
    def __init__(self, text: str, calls: list) -> None:
        self._text = text
        self._calls = calls

    def create(self, **kwargs):
        self._calls.append(kwargs)
        return _FakeMsg(self._text)


class _FakeClient:
    reply = "[]"
    create_calls: list = []
    last_kwargs: dict | None = None

    def __init__(self, **kwargs) -> None:
        _FakeClient.last_kwargs = kwargs
        self.messages = _FakeMessages(_FakeClient.reply, _FakeClient.create_calls)


def _install_fake_anthropic(monkeypatch, reply: str = "[]") -> type[_FakeClient]:
    _FakeClient.reply = reply
    _FakeClient.create_calls = []
    _FakeClient.last_kwargs = None
    fake = types.ModuleType("anthropic")
    fake.Anthropic = _FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return _FakeClient


# ── dry_run 더미 · 게이팅 ────────────────────────────────────────────────────
def test_dry_run_dummies_deterministic() -> None:
    s = _s(dry_run=True)
    seg = Segment(country="KR", industry="제약")
    a = AiDirectorySource(s).discover(seg)
    b = AiDirectorySource(s).discover(seg)
    assert len(a) == 2
    assert all(c.source == "ai_directory" for c in a)
    assert all(c.canonical_key.startswith("dom:") for c in a)  # 도메인 기반 키
    assert [c.canonical_key for c in a] == [c.canonical_key for c in b]  # 결정적
    assert all(c.industry == "제약·바이오" for c in a)  # 구체 업종 택소노미 정규화


def test_applies_only_to_specific_industry() -> None:
    src = AiDirectorySource(_s(dry_run=True))
    assert src.applies_to(Segment(country="US", industry="제약"))
    assert src.applies_to(Segment(country="DE", industry="반도체·디스플레이"))
    assert not src.applies_to(Segment(country="US", industry="전체"))  # broad 제외
    assert not src.applies_to(Segment(country="US", industry="기타"))


# ── opt-in / 인증 게이팅(no-op) ──────────────────────────────────────────────
def test_live_disabled_is_noop() -> None:
    s = _s(dry_run=False, ai_directory_source=False, anthropic_api_key="k", serper_api_key="x")
    assert AiDirectorySource(s).discover(Segment(country="US", industry="제약")) == []


def test_live_no_auth_is_noop() -> None:
    s = _s(dry_run=False, ai_directory_source=True, serper_api_key="x")  # anthropic 인증 없음
    assert AiDirectorySource(s).discover(Segment(country="US", industry="제약")) == []


# ── 라이브 경로: 디렉토리 URL → LLM JSON → 후보 ─────────────────────────────
def _live_source(monkeypatch, reply: str, ledger=None, **over):
    _install_fake_anthropic(monkeypatch, reply=reply)
    organic = [{"link": "https://dir.example.com/list", "title": "Pharma directory"}]
    pages = {"https://dir.example.com/list": "<p>Acme Pharma · Beta Bio</p>"}
    fetcher = _FakeFetcher(organic, pages)
    s = _s(
        dry_run=False, ai_directory_source=True, anthropic_api_key="k",
        serper_api_key="x", ai_directory_max_pages=1, **over,
    )
    src = AiDirectorySource(s, fetcher=fetcher, cost_ledger=ledger)
    return src, fetcher


def test_live_extracts_candidates(monkeypatch) -> None:
    reply = json.dumps([
        {"name": "Acme Pharma", "domain": "acme.com"},
        {"name": "Beta Bio", "domain": "https://www.beta.io/about"},  # www·경로 정규화
    ])
    ledger = _Ledger()
    src, fetcher = _live_source(monkeypatch, reply, ledger=ledger)
    rows = src.discover(Segment(country="US", industry="제약"))
    assert {r.domain for r in rows} == {"acme.com", "beta.io"}
    assert all(r.source == "ai_directory" for r in rows)
    assert all(r.canonical_key.startswith("dom:") for r in rows)
    assert fetcher.fetched == ["https://dir.example.com/list"]  # 페이지 1건 fetch
    assert "serper" in ledger.records  # 디렉토리 URL 수집 과금
    assert "ai_directory" in ledger.records  # LLM 추출 1페이지 과금


def test_live_bad_json_graceful_skip(monkeypatch) -> None:
    # 오염 응답(비-JSON) → 그 페이지 빈 산출, 하지만 왕복은 성공했으니 과금은 됨.
    ledger = _Ledger()
    src, _ = _live_source(monkeypatch, "죄송하지만 도울 수 없습니다 {{{ not json", ledger=ledger)
    rows = src.discover(Segment(country="US", industry="제약"))
    assert rows == []
    assert "ai_directory" in ledger.records


def test_live_rejects_garbage_domain(monkeypatch) -> None:
    # 인젝션 안전: 정상 도메인만 채택, 비정상 문자 덩어리(%·!)는 형식 검증에서 거부.
    reply = json.dumps([
        {"name": "Legit Co", "domain": "legit.com"},
        {"name": "Bad1", "domain": "ev!l.com"},          # 형식 위반(!)
        {"name": "Bad2", "domain": "%2f%2f" * 500},      # 인코딩 덩어리(도메인 아님)
    ])
    src, _ = _live_source(monkeypatch, reply)
    rows = src.discover(Segment(country="US", industry="제약"))
    assert {r.domain for r in rows} == {"legit.com"}


def test_live_respects_discovery_cap(monkeypatch) -> None:
    reply = json.dumps([{"name": f"Co{i}", "domain": f"co{i}.com"} for i in range(5)])
    src, _ = _live_source(monkeypatch, reply, discovery_max_per_source=2)
    rows = src.discover(Segment(country="US", industry="제약"))
    assert len(rows) == 2  # 캡에서 정지


# ── 추출기 단위: 과금 가드(예산·런당캡) ──────────────────────────────────────
def test_extractor_budget_over_skips_call(monkeypatch) -> None:
    fc = _install_fake_anthropic(monkeypatch, reply='[{"name":"A","domain":"a.com"}]')
    ledger = _Ledger(over=True)
    ex = ClaudeDirectoryExtractor(model="m", api_key="k", ledger=ledger)
    assert ex.extract("제약", "some text") == []
    assert fc.create_calls == []  # 예산초과 → 호출 자체 안 함
    assert ledger.records == []  # 미과금


def test_extractor_max_calls_cap(monkeypatch) -> None:
    _install_fake_anthropic(monkeypatch, reply='[{"name":"A","domain":"a.com"}]')
    ledger = _Ledger()
    ex = ClaudeDirectoryExtractor(model="m", api_key="k", ledger=ledger, max_calls=2)
    assert ex.extract("제약", "t")  # 1
    assert ex.extract("제약", "t")  # 2
    assert ex.extract("제약", "t") == []  # 캡 초과 → 호출 없음
    assert ledger.records == ["ai_directory", "ai_directory"]  # 2건만 과금


def test_extractor_uses_auth_token_bearer(monkeypatch) -> None:
    fc = _install_fake_anthropic(monkeypatch, reply='[{"name":"A","domain":"a.com"}]')
    ex = ClaudeDirectoryExtractor(model="m", auth_token="oat-1")
    assert ex.extract("제약", "t")[0].domain == "a.com"
    assert fc.last_kwargs.get("auth_token") == "oat-1"  # Bearer 인증
    assert "api_key" not in fc.last_kwargs


# ── 파서·헬퍼 순수함수 ───────────────────────────────────────────────────────
def test_parse_companies_extracts_array_from_noise() -> None:
    raw = '```json\n[{"name":"A","domain":"a.com"},{"bad":1},"x"]\n```'
    got = _parse_companies(raw)
    assert [c.domain for c in got] == ["a.com"]  # 유효 원소만


def test_parse_companies_bad_returns_empty() -> None:
    assert _parse_companies("설명만 있고 배열 없음") == []
    assert _parse_companies("[not valid json}") == []


def test_english_country_name() -> None:
    assert _english_country_name(resolve_country("KR"), "KR") == "Korea"
    assert _english_country_name(resolve_country("US"), "US") == "United States"
    assert _english_country_name(None, "우주국") == "우주국"  # 미등록 → 원문


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
