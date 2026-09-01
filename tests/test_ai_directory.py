"""AI 디렉토리 발견 소스 테스트 — dry_run 더미·게이팅·라이브 경로·인젝션 안전·과금가드.

네트워크·과금 없이 통과한다: SERP·페이지 fetch 는 페이크 페처로, LLM 은 sys.modules 에
주입한 페이크 anthropic 으로 대체한다(:mod:`test_industry_classify` 패턴).
"""

from __future__ import annotations

import json
import socket
import sys
import types

import pytest

from leadcrawler.config import Settings
from leadcrawler.sources.ai_directory import (
    AiDirectorySource,
    ClaudeDirectoryExtractor,
    _english_country_name,
    _is_safe_public_url,
    _parse_companies,
)
from leadcrawler.sources.base import Segment
from leadcrawler.sources.countries import resolve_country
from leadcrawler.sources.http import Fetcher


def _fake_getaddrinfo(ip: str):
    """socket.getaddrinfo 대체 — 항상 ``ip`` 를 돌린다(네트워크·DNS 없이 SSRF 검증 시뮬)."""
    def _f(host, *_a, **_k):  # noqa: ARG001
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _f


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

    def get_text(  # noqa: ARG002
        self, url, *, params=None, headers=None, allow_redirects=True, max_bytes=None
    ):
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
    # SERP 링크의 SSRF 검증(getaddrinfo)을 네트워크 없이 공인 IP 로 시뮬(example.com 실 IP).
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
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


# ── SSRF 방어: URL 검증(getaddrinfo monkeypatch — 네트워크 없이) ──────────────
def test_is_safe_public_url_accepts_public(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert _is_safe_public_url("https://good.example.com/path?q=1")
    assert _is_safe_public_url("http://good.example.com")


def test_is_safe_public_url_rejects_bad_scheme(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert not _is_safe_public_url("ftp://good.example.com")
    assert not _is_safe_public_url("file:///etc/passwd")
    assert not _is_safe_public_url("javascript:alert(1)")
    assert not _is_safe_public_url("not a url")


def test_is_safe_public_url_rejects_private_and_meta(monkeypatch) -> None:
    # 사설·루프백·링크로컬(클라우드 메타데이터)·미지정 대역은 거부(내부망 피벗 차단).
    for ip in ("127.0.0.1", "10.1.2.3", "192.168.0.5", "169.254.169.254", "0.0.0.0"):
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(ip))
        assert not _is_safe_public_url("https://sneaky.example.com"), ip


def test_is_safe_public_url_rejects_dns_failure(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise socket.gaierror("nxdomain")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    assert not _is_safe_public_url("https://nxdomain.invalid")


def test_collect_urls_skips_private_link(monkeypatch) -> None:
    # 통합: SERP 링크가 사설 IP 로 해석되면 fetch 대상에서 제외돼 산출 0(과금·fetch 없음).
    src, fetcher = _live_source(monkeypatch, json.dumps([{"name": "A", "domain": "a.com"}]))
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))  # 사설로 전환
    rows = src.discover(Segment(country="US", industry="제약"))
    assert rows == []
    assert fetcher.fetched == []  # 검증 탈락 → 페이지 fetch 자체 안 함


# ── max_bytes 스트림 절단(Fetcher, 페이크 httpx client) ───────────────────────
class _FakeStreamResp:
    def __init__(self, chunks: list[bytes], encoding: str = "utf-8") -> None:
        self._chunks = chunks
        self.encoding = encoding
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *_a) -> bool:
        return False


class _FakeHttpClient:
    def __init__(self, chunks: list[bytes], text: str = "FULLBODY") -> None:
        self._chunks = chunks
        self._text = text

    def get(self, url, **_kw):  # noqa: ARG002
        return types.SimpleNamespace(text=self._text, raise_for_status=lambda: None)

    def stream(self, method, url, **_kw):  # noqa: ARG002
        return _FakeStreamResp(self._chunks)


def test_get_text_max_bytes_truncates() -> None:
    f = Fetcher(min_interval=0.0)
    f._client = _FakeHttpClient([b"x" * 1000, b"y" * 1000])  # type: ignore[assignment]
    out = f.get_text("http://x", max_bytes=1500)
    assert len(out) == 1500  # 상한에서 절단
    # 무인자 호출(회귀): max_bytes None → 스트림 아닌 .text 경로 그대로.
    assert f.get_text("http://x") == "FULLBODY"


# ── max_tokens 설정이 extractor→anthropic 으로 전달되는지 ─────────────────────
def test_max_tokens_setting_passed_through(monkeypatch) -> None:
    src, _ = _live_source(
        monkeypatch, json.dumps([{"name": "A", "domain": "a.com"}]),
        ai_directory_max_tokens=1234,
    )
    src.discover(Segment(country="US", industry="제약"))
    assert _FakeClient.create_calls[0]["max_tokens"] == 1234


def test_ai_directory_max_tokens_default() -> None:
    assert _s().ai_directory_max_tokens == 4096


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
