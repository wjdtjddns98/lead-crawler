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


class _CountingSite:
    """head_ok 호출 횟수를 센다(중복 HTTP 왕복 제거 검증용)."""

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls = 0

    def head_ok(self, domain: str) -> bool:
        self.calls += 1
        return self.ok


# 정상 home 본문(링크·풍부한 텍스트 → looks_parked=False) / 파킹 본문(판매 표지+빈약 본문).
_LIVE_HOME = (
    "<html><body><a href='/ir'>Investor Relations</a>"
    "<p>Welcome to Acme Corporation, a global leader in industrial systems.</p></body></html>"
)
_PARKED_HOME = "<html><body>This domain is for sale.</body></html>"


# --- 후속 C: enrich home 신호 재사용(중복 HTTP 프로브 제거) -----------------

def test_home_html_skips_head_ok_probe() -> None:
    # enrich 가 home 을 200 GET 함(home_html 제공) → existence 는 head_ok 를 안 쏜다(중복 제거).
    site = _CountingSite(True)
    v = ExistenceVerifier(Settings(dry_run=False), site_probe=site, dns_probe=_Dns(True))
    r = v.verify("acme.com", home_html=_LIVE_HOME)
    assert site.calls == 0  # HEAD/GET 프로브 생략
    assert r.is_active and r.site_alive and r.confidence == 0.85  # head_ok=True+dns 와 동치


def test_no_home_html_falls_back_to_head_ok() -> None:
    # home_html None(enrich dry/실패/도메인없음) → 기존 head_ok 경로 그대로.
    site = _CountingSite(True)
    v = ExistenceVerifier(Settings(dry_run=False), site_probe=site, dns_probe=_Dns(True))
    r = v.verify("acme.com", home_html=None)
    assert site.calls == 1  # 프로브 수행
    assert r.is_active and r.confidence == 0.85


def test_home_html_output_matches_head_ok_path() -> None:
    # 정상 home_html 경로(프로브 생략)와 head_ok=True 경로의 산출이 동치(순수 중복제거).
    reused = ExistenceVerifier(
        Settings(dry_run=False), site_probe=_CountingSite(True), dns_probe=_Dns(True)
    ).verify("acme.com", home_html=_LIVE_HOME)
    probed = ExistenceVerifier(
        Settings(dry_run=False), site_probe=_Site(True), dns_probe=_Dns(True)
    ).verify("acme.com")
    assert (reused.is_active, reused.site_alive, reused.confidence) == (
        probed.is_active, probed.site_alive, probed.confidence
    )


def test_parked_home_html_is_not_alive() -> None:
    # M-1: enrich 가 GET 200 받았어도 본문이 파킹이면 실존 아님(제약② — head_ok GET폴백 동치).
    site = _CountingSite(True)
    v = ExistenceVerifier(Settings(dry_run=False), site_probe=site, dns_probe=_Dns(True))
    r = v.verify("parked.com", home_html=_PARKED_HOME)
    assert site.calls == 0  # 프로브는 여전히 생략(중복 왕복 0)
    assert not r.is_active and not r.site_alive and r.confidence == 0.0  # 파킹 → reject


def test_home_html_ignored_when_no_domain() -> None:
    # 도메인 없으면 home_html 이 있어도 site_alive=False(기존 계약 보존).
    site = _CountingSite(True)
    v = ExistenceVerifier(Settings(dry_run=False), site_probe=site, dns_probe=_Dns(True))
    r = v.verify(None, home_html=_LIVE_HOME)
    assert site.calls == 0 and not r.is_active and r.confidence == 0.0


# bare SPA 셸 — 정적 본문이 JS-blank(텍스트<20·a/img 없음) → looks_parked=True(정적상 모호).
_BLANK_SPA_HOME = "<html><body><div id='root'></div></body></html>"


def test_blank_spa_home_html_rejected_without_headless() -> None:
    # verify_headless OFF(기본): 정적 JS-blank 는 최종 비생존(제약② 강화, 정적으로는 확인불가).
    v = ExistenceVerifier(
        Settings(dry_run=False, verify_headless=False), dns_probe=_Dns(True)
    )
    r = v.verify("spa.com", home_html=_BLANK_SPA_HOME)
    assert not r.is_active and not r.site_alive


def test_blank_spa_home_html_rescued_by_headless() -> None:
    # verify_headless ON: 정적 파킹/blank 의심분은 단정 않고 렌더로 최종판정 → 정상 SPA 구제.
    render = _Render("<html><body><a href='/ir'>IR</a><p>Live rendered company site.</p></body></html>")
    v = ExistenceVerifier(
        Settings(dry_run=False, verify_headless=True), dns_probe=_Dns(True), render_probe=render
    )
    r = v.verify("spa.com", home_html=_BLANK_SPA_HOME)
    assert r.is_active and r.site_alive and render.calls == 1  # 렌더가 살림


def test_parked_home_html_with_headless_rejected_by_render() -> None:
    # verify_headless ON 이어도 렌더 본문이 파킹이면 비생존(렌더가 최종 정정).
    render = _Render("<html><body>this domain is parked</body></html>")
    v = ExistenceVerifier(
        Settings(dry_run=False, verify_headless=True), dns_probe=_Dns(True), render_probe=render
    )
    r = v.verify("parked.com", home_html=_PARKED_HOME)
    assert not r.is_active and render.calls == 1


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


def test_registrar_with_marker_but_rich_content_not_parked() -> None:
    # 리뷰어 MEDIUM-1 회귀: '주차된 도메인'을 제품명으로 쓰는 레지스트라(가비아 등) 정상 홈페이지.
    # 마커가 있어도 본문이 풍부하면 파킹 아님(제약② 리드손실 방지).
    body = (
        "<nav>홈 서비스소개 도메인 호스팅 서버 보안 고객센터 마이페이지 로그인 회원가입</nav>"
        "<h1>가비아 — 대한민국 1위 인터넷 인프라 서비스</h1>"
        "<section>도메인 등록과 이전, 웹호스팅, 클라우드 서버, 매니지드 서비스, SSL 인증서, 기업메일까지 "
        "한 곳에서 제공합니다. 부가 상품 중 '주차된 도메인' 관리 기능으로 미사용 도메인을 손쉽게 운영할 수 있습니다. "
        "최신 클라우드 인프라와 24시간 365일 기술지원, 안정적인 백본망을 바탕으로 수십만 고객사가 신뢰합니다. "
        "스타트업부터 대기업까지 규모에 맞는 요금제와 전담 컨설팅을 제공하며, 데이터센터 이중화로 무중단 운영을 보장합니다.</section>"
        "<footer>회사소개 채용 투자정보 약관 개인정보처리방침 이용안내 제휴문의 공지사항</footer>"
    )
    assert looks_parked(f"<html><body>{body}</body></html>") is False


def test_image_only_homepage_not_blank() -> None:
    # 리뷰어 MEDIUM-2 회귀: 텍스트 적은 이미지-only 소규모 정상 홈페이지(img 구조 신호로 보존).
    assert looks_parked('<html><body><h1>OO</h1><img src="hero.jpg"><a href="/about">회사</a></body></html>') is False


def test_js_blank_spa_is_parked() -> None:
    # 리뷰어 LOW-1 회귀: script 내용 제거 후 본문 0 + 구조 없음 → JS-blank 죽음 처리.
    spa = '<html><head><script>var x=' + "1;" * 200 + '</script></head><body><div id="root"></div></body></html>'
    assert looks_parked(spa) is True


def test_js_blank_spa_with_anchor_string_in_script_still_parked() -> None:
    # 리뷰어 LOW-1/LOW-3: 스크립트가 '<a href' 문자열을 포함해도(가시 구조 아님) blank 로 잡혀야.
    spa = '<html><head><script>var link="<a href=x>";' + "y;" * 100 + '</script></head><body></body></html>'
    assert looks_parked(spa) is True


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


def test_head_403_waf_falls_back_to_get(monkeypatch) -> None:
    # WAF/안티봇이 HEAD 에 403 → GET 폴백으로 생존 회복(false-negative 리드손실 방지).
    calls = _patch_head_get(
        monkeypatch, head_status=403,
        get_resp=_GetResp(200, "<html><body><p>실제 회사 홈페이지 콘텐츠가 충분히 깁니다.</p></body></html>"),
    )
    assert HttpSiteProbe().head_ok("acme.com") is True
    assert calls["get"] == 1


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
