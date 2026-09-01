"""실존성 검증 테스트 — 다중 신호 등급화(주입형 프로브, 네트워크 없음)."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.verify.existence import (
    DnsProbe,
    ExistenceVerifier,
    _host_variants,
)


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


def test_parked_home_html_reprobed_dead_stays_dead() -> None:
    # M-1: 본문이 파킹으로 보이면 프로브로 재확인 — 프로브도 죽음이면 그대로 reject(제약②).
    site = _CountingSite(False)
    v = ExistenceVerifier(Settings(dry_run=False), site_probe=site, dns_probe=_Dns(True))
    r = v.verify("parked.com", home_html=_PARKED_HOME)
    assert site.calls == 1  # 파킹 의심 → 프로브 재확인 1회(브라우저 UA GET+파킹 가드).
    assert not r.is_active and not r.site_alive and r.confidence == 0.0


def test_parked_looking_home_html_rescued_by_probe() -> None:
    # 크롤러 UA 로 받은 WAF 챌린지 셸은 파킹과 동형 — 프로브(브라우저 UA)가 살아있다고
    # 판정하면 생존(2026-08-25 공제회 실측 오탐 구제). 진짜 파킹은 프로브도 파킹이라 불변.
    site = _CountingSite(True)
    v = ExistenceVerifier(Settings(dry_run=False), site_probe=site, dns_probe=_Dns(True))
    r = v.verify("waf-shell.co.kr", home_html=_PARKED_HOME)
    assert site.calls == 1
    assert r.is_active and r.site_alive


def test_home_html_ignored_when_no_domain() -> None:
    # 도메인 없으면 home_html 이 있어도 site_alive=False(기존 계약 보존).
    site = _CountingSite(True)
    v = ExistenceVerifier(Settings(dry_run=False), site_probe=site, dns_probe=_Dns(True))
    r = v.verify(None, home_html=_LIVE_HOME)
    assert site.calls == 0 and not r.is_active and r.confidence == 0.0


# bare SPA 셸 — 정적 본문이 JS-blank(텍스트<20·a/img 없음) → looks_parked=True(정적상 모호).
_BLANK_SPA_HOME = "<html><body><div id='root'></div></body></html>"


def test_blank_spa_home_html_rejected_without_headless() -> None:
    # verify_headless OFF(기본): 정적 JS-blank 는 프로브 재확인까지 죽음이면 최종 비생존.
    v = ExistenceVerifier(
        Settings(dry_run=False, verify_headless=False),
        site_probe=_CountingSite(False),
        dns_probe=_Dns(True),
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


# --- 등록처 active = confidence 보강만(admit override 아님, 제약 ②) ----------

def test_registry_active_does_not_admit_dead_site() -> None:
    # 등록처 active 여도 사이트가 죽으면 실존 아님 — admit 은 site_alive 필수(제약 ②:
    # active + 도메인 생존). 등록만 되고 사이트 죽은·406·파킹 CH 휴면·셸을 큐에서 제외한다.
    r = _verify(site=False, dns=False, reg=True)
    assert not r.is_active and r.confidence == 0.0


def test_registry_active_boosts_confidence_on_live_site() -> None:
    # 등록처 active + 사이트 생존 = 최강 실존(0.9). active 는 admit 이 아니라 confidence 보강.
    r = _verify(site=True, dns=False, reg=True)
    assert r.is_active and r.site_alive and r.confidence == 0.9


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

    monkeypatch.setattr(httpx.Client, "head", lambda self, url, **k: _Resp())
    assert HttpSiteProbe().head_ok("acme.com") is True

    def _boom(self, url, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.Client, "head", _boom)
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

    def fake_head(self, url, **kw):
        calls["head"] += 1
        return _GetResp(head_status)

    def fake_get(self, url, **kw):
        calls["get"] += 1
        if get_resp is None:
            raise RuntimeError("refused")
        return get_resp

    monkeypatch.setattr(httpx.Client, "head", fake_head)
    monkeypatch.setattr(httpx.Client, "get", fake_get)
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
        self.closed = False

    def render(self, domain: str) -> str | None:
        self.calls += 1
        return self.html

    def close(self) -> None:
        self.closed = True


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


def test_headless_reuses_enrich_rendered_html_no_own_render() -> None:
    # enrich 가 넘긴 렌더 HTML(파킹)로 판정 → 자체 render 호출 0(기업당 Chromium 중복 제거).
    render = _Render("<html><body><p>자체 렌더는 호출되면 안 됨 — 살아있는 본문 IR 공시.</p></body></html>")
    v = ExistenceVerifier(
        Settings(dry_run=False, verify_headless=True),
        site_probe=_Site(True), dns_probe=_Dns(True), render_probe=render,
    )
    res = v.verify("acme.com", rendered_html="<html><body>buy this domain</body></html>")
    assert res.is_active is False  # 재사용한 렌더 HTML 이 파킹 → 비생존.
    assert render.calls == 0  # 자체 렌더 생략.


def test_headless_renders_when_no_rendered_html_provided() -> None:
    # rendered_html=None(enrich 미렌더) → 기존대로 자체 렌더(회귀 0).
    render = _Render("<html><body>buy this domain</body></html>")
    v = ExistenceVerifier(
        Settings(dry_run=False, verify_headless=True),
        site_probe=_Site(True), dns_probe=_Dns(True), render_probe=render,
    )
    res = v.verify("acme.com", rendered_html=None)
    assert render.calls == 1 and res.is_active is False


def test_headless_dry_run_skips_render() -> None:
    render = _Render("buy this domain")
    v = ExistenceVerifier(Settings(dry_run=True, verify_headless=True), render_probe=render)
    assert v.verify("acme.com").is_active is True and render.calls == 0


# === D: PlaywrightRender 브라우저 재사용(콜드스타트 제거) ====================

class _FakePage:
    def __init__(self, fail_schemes: tuple[str, ...] = ()) -> None:
        self.fail = set(fail_schemes)
        self.gotos: list[str] = []
        self.closed = False

    def goto(self, url, timeout, wait_until):  # noqa: ANN001, ARG002
        self.gotos.append(url)
        if url.split("://", 1)[0] in self.fail:
            raise RuntimeError("nav fail")

    def content(self) -> str:
        return "<html>rendered</html>"

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.new_page_calls = 0
        self.closed = False

    def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        return self._page

    def close(self) -> None:
        self.closed = True


def test_render_reuses_browser_across_calls() -> None:
    from leadcrawler.verify.existence import PlaywrightRender

    r = PlaywrightRender(timeout=1.0)
    page = _FakePage()
    browser = _FakeBrowser(page)
    r._browser = browser  # 기동 우회(_ensure True) — 재사용 검증.
    r._context = browser  # 컨텍스트 재사용 구조(new_page 는 컨텍스트 소관) — 페이크 겸용.
    assert r.render("a.com") == "<html>rendered</html>"
    assert r.render("b.com") == "<html>rendered</html>"
    assert browser.new_page_calls == 2  # 페이지는 매 호출 생성
    assert browser.closed is False  # 브라우저는 재사용(닫지 않음)
    r.close()
    assert browser.closed is True  # close() 가 정리


def test_render_falls_back_https_to_http() -> None:
    from leadcrawler.verify.existence import PlaywrightRender

    r = PlaywrightRender()
    page = _FakePage(fail_schemes=("https",))
    r._browser = r._context = _FakeBrowser(page)  # 컨텍스트 재사용 구조 — 페이크 겸용.
    assert r.render("a.com") == "<html>rendered</html>"
    assert page.gotos == ["https://a.com", "http://a.com"]  # https 실패→http 폴백
    assert page.closed is True  # 페이지 정리(finally)


def test_render_caches_unavailability() -> None:
    from leadcrawler.verify.existence import PlaywrightRender

    r = PlaywrightRender()
    r._unavailable = True  # 미설치/기동실패 캐시 → 재시도 안 함.
    assert r._ensure() is False
    assert r.render("a.com") is None


def test_verifier_close_propagates_to_render_probe() -> None:
    # ExistenceVerifier.close() 가 재사용 렌더러 자원정리(close)를 전파한다(브라우저 누수 방지).
    render = _Render("<html>x</html>")
    v = ExistenceVerifier(Settings(dry_run=False, verify_headless=True), render_probe=render)
    v.close()  # close() 는 주입된 프로브를 순회 정리(네트워크 불필요).
    assert render.closed is True


# --- www-only 사이트 구제(bare 실패 시 www. 프로브) -----------------------

def test_host_variants_adds_www() -> None:
    assert _host_variants("acme.co.kr") == ("acme.co.kr", "www.acme.co.kr")
    assert _host_variants("www.acme.co.kr") == ("www.acme.co.kr",)  # 이미 www 면 하나만.


def test_head_ok_falls_back_to_www(monkeypatch) -> None:
    """bare 도메인은 연결 실패, www 는 200 → head_ok True(www-only 사이트 구제)."""
    import httpx

    tried: list[str] = []

    class _Resp:
        status_code = 200

    def fake_head(self, url, **_kw):
        tried.append(url)
        if "www." not in url:
            raise httpx.ConnectError("no A record on bare")
        return _Resp()

    monkeypatch.setattr(httpx.Client, "head", fake_head)
    assert HttpSiteProbe().head_ok("acme.co.kr") is True
    assert any("www.acme.co.kr" in u for u in tried)  # www 변형을 실제로 시도했다.


def test_dns_resolves_falls_back_to_www(monkeypatch) -> None:
    """bare 는 A·MX 없음, www 에만 A → resolves True."""
    import dns.resolver

    def fake_resolve(host, rtype):
        if host.startswith("www.") and rtype == "A":
            return ["1.2.3.4"]
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "resolve", fake_resolve)
    assert DnsProbe().resolves("acme.co.kr") is True


def test_site_probe_head_400_falls_back_to_get(monkeypatch) -> None:
    # 일부 WAF 는 HEAD 를 400 으로 거절한다(2026-08-25 실측) — GET 폴백으로 생존 판정.
    import httpx

    from leadcrawler.verify.existence import HttpSiteProbe

    calls: list[str] = []

    def fake_head(self, url, **kw):
        calls.append(f"HEAD {url}")
        return httpx.Response(400, request=httpx.Request("HEAD", url))

    def fake_get(self, url, **kw):
        calls.append(f"GET {url}")
        return httpx.Response(
            200, request=httpx.Request("GET", url),
            text="<html><body>공제회 공식 홈페이지 안내와 사업 소개</body></html>",
        )

    monkeypatch.setattr(httpx.Client, "head", fake_head)
    monkeypatch.setattr(httpx.Client, "get", fake_get)
    assert HttpSiteProbe().head_ok("pmaa.or.kr") is True
    assert any(c.startswith("GET ") for c in calls)


def test_site_probe_sends_browser_ua(monkeypatch) -> None:
    # 기본 httpx UA 는 WAF 타르핏 오탐 원인 — 프로브는 브라우저형 UA 를 보낸다.
    import httpx

    from leadcrawler.verify.existence import HttpSiteProbe

    seen: dict = {}
    clients: list[int] = []

    def fake_head(self, url, **kw):
        seen["headers"] = self.headers  # Client 생성 시 고정된 헤더.
        clients.append(id(self))
        return httpx.Response(200, request=httpx.Request("HEAD", url))

    monkeypatch.setattr(httpx.Client, "head", fake_head)
    probe = HttpSiteProbe()
    assert probe.head_ok("example.co.kr") is True
    assert probe.head_ok("example2.co.kr") is True
    assert "Mozilla" in seen["headers"].get("User-Agent", "")
    assert len(set(clients)) == 1  # 프로버 수명 동안 Client 1개 재사용(호출마다 재생성 X).
    probe.close()
    assert probe._client is None
