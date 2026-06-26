"""실존성 검증 테스트 — 다중 신호 등급화(주입형 프로브, 네트워크 없음)."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.verify.existence import ExistenceVerifier


class _Site:
    def __init__(self, ok: bool) -> None:
        self.ok = ok

    def head_ok(self, domain: str) -> bool:
        return self.ok


class _Dns:
    def __init__(self, ok: bool) -> None:
        self.ok = ok

    def resolves(self, domain: str) -> bool:
        return self.ok


class _Reg:
    def __init__(self, val: bool | None) -> None:
        self.val = val

    def is_active(self, registry, registry_id):
        return self.val


def _verify(site: bool, dns: bool, *, reg: bool | None = None, domain: str = "acme.com"):
    v = ExistenceVerifier(
        Settings(dry_run=False),
        site_probe=_Site(site),
        dns_probe=_Dns(dns),
        registry_checker=_Reg(reg) if reg is not None else None,
    )
    return v.verify(domain, registry="edgar", registry_id="0001")


# --- dry_run -----------------------------------------------------------

def test_dry_run_active_with_domain() -> None:
    r = ExistenceVerifier(Settings(dry_run=True)).verify("acme.com")
    assert r.is_active and r.site_alive and r.confidence == 0.9


def test_dry_run_inactive_without_domain() -> None:
    r = ExistenceVerifier(Settings(dry_run=True)).verify(None)
    assert not r.is_active and r.confidence == 0.0


# --- 라이브 등급화(주입 프로브) ---------------------------------------

def test_both_signals_high_confidence() -> None:
    r = _verify(site=True, dns=True)
    assert r.is_active and r.site_alive and r.confidence == 0.85


def test_http_only_mid_confidence() -> None:
    r = _verify(site=True, dns=False)
    assert r.is_active and r.site_alive and r.confidence == 0.7


def test_dns_only_inactive() -> None:
    # DNS 만 해석되고 사이트가 죽었으면 비실존(parked domain 보수 처리, 제약 ②).
    r = _verify(site=False, dns=True)
    assert not r.is_active and not r.site_alive and r.confidence == 0.0


def test_no_signal_inactive() -> None:
    r = _verify(site=False, dns=False)
    assert not r.is_active and r.confidence == 0.0


def test_no_domain_inactive() -> None:
    r = _verify(site=True, dns=True, domain="")
    assert not r.is_active and r.confidence == 0.0  # 도메인 없으면 프로브 미시도.


# --- 등록처 active 신호 우선 ------------------------------------------

def test_registry_active_overrides_dead_site() -> None:
    # 등록처가 active 면 사이트·DNS 가 죽어도 실존으로 본다.
    r = _verify(site=False, dns=False, reg=True)
    assert r.is_active and r.confidence == 0.9


def test_registry_defunct_overrides_live_site() -> None:
    # 등록처가 defunct 면 사이트가 살아도 실존 아님(제약 ②). 높은 신뢰(0.9).
    r = _verify(site=True, dns=True, reg=False)
    assert not r.is_active and r.site_alive and r.confidence == 0.9


# --- 실 프로버 단위(monkeypatch, 네트워크 없음) -----------------------

def test_http_probe_ok_and_fail(monkeypatch) -> None:
    import httpx

    from leadcrawler.verify.existence import HttpSiteProbe

    class _Resp:
        status_code = 200

    monkeypatch.setattr(httpx, "head", lambda url, **k: _Resp())
    assert HttpSiteProbe().head_ok("acme.com") is True

    def _boom(url, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "head", _boom)
    assert HttpSiteProbe().head_ok("dead.com") is False


def test_dns_probe_resolves_and_fails(monkeypatch) -> None:
    import dns.resolver

    from leadcrawler.verify.existence import DnsProbe

    monkeypatch.setattr(dns.resolver, "resolve", lambda d, rt: ["1.2.3.4"])
    assert DnsProbe().resolves("acme.com") is True

    def _noanswer(d, rt):
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "resolve", _noanswer)
    assert DnsProbe().resolves("nope.invalid") is False


# === Track B: 파킹 휴리스틱 ============================================

from leadcrawler.verify.existence import HttpSiteProbe, looks_parked  # noqa: E402


def test_parked_markers_detected() -> None:
    assert looks_parked("<html><body>This domain is parked. Buy this domain!</body></html>")
    assert looks_parked("<h1>this domain is for sale</h1> 부가 텍스트를 충분히 채워 길이 통과")


def test_blank_or_short_is_parked() -> None:
    assert looks_parked("") is True
    assert looks_parked("   ") is True
    assert looks_parked("<html><body></body></html>") is True  # 본문 텍스트 0
    assert looks_parked(None) is True


def test_real_content_not_parked() -> None:
    html = "<html><body><h1>삼성전자 IR</h1><p>투자자 정보와 재무제표, 공시 자료를 제공합니다.</p></body></html>"
    assert looks_parked(html) is False


# === Track B: HEAD 405 → GET 폴백(B2) =================================

class _GetResp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _patch_head_get(monkeypatch, *, head_status: int, get_resp: _GetResp | None) -> dict:
    import httpx

    calls = {"head": 0, "get": 0}

    def fake_head(url, **kw):
        calls["head"] += 1
        return _GetResp(head_status)

    def fake_get(url, **kw):
        calls["get"] += 1
        if get_resp is None:
            raise RuntimeError("refused")
        return get_resp

    monkeypatch.setattr(httpx, "head", fake_head)
    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


def test_head_405_falls_back_to_get_alive(monkeypatch) -> None:
    calls = _patch_head_get(
        monkeypatch, head_status=405,
        get_resp=_GetResp(200, "<html><body><p>실제 회사 홈페이지 콘텐츠가 충분히 깁니다.</p></body></html>"),
    )
    assert HttpSiteProbe().head_ok("acme.com") is True
    assert calls["head"] == 1 and calls["get"] == 1


def test_head_405_get_parked_is_dead(monkeypatch) -> None:
    _patch_head_get(monkeypatch, head_status=405, get_resp=_GetResp(200, "This domain is parked"))
    assert HttpSiteProbe().head_ok("acme.com") is False


def test_head_200_skips_get(monkeypatch) -> None:
    calls = _patch_head_get(monkeypatch, head_status=200, get_resp=None)
    assert HttpSiteProbe().head_ok("acme.com") is True
    assert calls["get"] == 0


# === Track B: 헤드리스 확인(B1) =======================================

class _Render:
    def __init__(self, html) -> None:
        self.html = html
        self.calls = 0

    def render(self, domain: str) -> str | None:
        self.calls += 1
        return self.html


def _headless_verify(html, *, headless: bool, site: bool = True):
    render = _Render(html)
    v = ExistenceVerifier(
        Settings(dry_run=False, verify_headless=headless),
        site_probe=_Site(site), dns_probe=_Dns(True), render_probe=render,
    )
    return v.verify("acme.com"), render


def test_headless_parked_marks_inactive() -> None:
    res, render = _headless_verify("<html><body>buy this domain</body></html>", headless=True)
    assert res.is_active is False and render.calls == 1


def test_headless_real_content_stays_active() -> None:
    html = "<html><body><h1>회사</h1><p>충분히 긴 실제 본문 콘텐츠가 여기 있습니다 IR 정보 공시.</p></body></html>"
    res, _ = _headless_verify(html, headless=True)
    assert res.is_active is True


def test_headless_render_none_is_graceful() -> None:
    # 렌더 실패(None) → 기존 HTTP 판정 유지(실존 기업 보존).
    res, _ = _headless_verify(None, headless=True)
    assert res.is_active is True


def test_headless_off_skips_render() -> None:
    res, render = _headless_verify("buy this domain", headless=False)
    assert res.is_active is True and render.calls == 0


def test_headless_dry_run_skips_render() -> None:
    render = _Render("buy this domain")
    v = ExistenceVerifier(Settings(dry_run=True, verify_headless=True), render_probe=render)
    assert v.verify("acme.com").is_active is True and render.calls == 0
