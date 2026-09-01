"""죽은 브라우저 리셋 검증 — 2026-07-31 백필 OOM(좀비 드라이버 누적)의 재발 방지.

브라우저/드라이버 사망 예외("has been closed" 등)를 받으면 렌더러가 자원을 리셋해
다음 호출이 재기동하게 하는지, 일반 렌더 실패에선 리셋하지 않는지를 브라우저 없이 검증.
"""

from __future__ import annotations

from leadcrawler.enrich.headless import PlaywrightRenderer, browser_dead
from leadcrawler.verify.existence import PlaywrightRender


class _Closable:
    def close(self) -> None:
        pass

    def stop(self) -> None:
        pass


class _DeadContext(_Closable):
    """브라우저 사망 상태 — new_page 가 Playwright 사망 메시지로 실패."""

    def new_page(self):
        raise RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed")


class _FlakyPage(_Closable):
    def goto(self, *a, **k) -> None:
        raise TimeoutError("Timeout 20000ms exceeded")


class _AliveContext(_Closable):
    """브라우저는 살아있고 goto 만 실패(타임아웃) — 리셋하면 안 된다."""

    def new_page(self):
        return _FlakyPage()


def test_browser_dead_판정():
    assert browser_dead(RuntimeError("Target page, context or browser has been closed"))
    assert browser_dead(RuntimeError("Connection closed while reading from the driver"))
    assert browser_dead(RuntimeError("Playwright connection closed"))  # 소문자 c 변형(리뷰 MED-1).
    assert browser_dead(RuntimeError("Target closed"))
    assert browser_dead(RuntimeError("Socket closed"))
    assert not browser_dead(TimeoutError("Timeout 20000ms exceeded"))
    assert not browser_dead(RuntimeError("net::ERR_NAME_NOT_RESOLVED"))


def test_route_filter_차단과_통과():
    from leadcrawler.enrich.headless import _route_filter

    class _Route:
        def __init__(self, rtype: str) -> None:
            self.request = type("R", (), {"resource_type": rtype})()
            self.aborted = self.continued = False

        def abort(self) -> None:
            self.aborted = True

        def continue_(self) -> None:
            self.continued = True

    for rtype in ("image", "media", "font"):
        r = _Route(rtype)
        _route_filter(r)
        assert r.aborted and not r.continued, rtype
    for rtype in ("document", "script", "xhr", "stylesheet"):
        r = _Route(rtype)
        _route_filter(r)
        assert r.continued and not r.aborted, rtype

    class _RacyRoute(_Route):
        def continue_(self) -> None:  # page.close() 와 경합 — 예외를 삼켜야 한다(리뷰 MED-2).
            raise RuntimeError("Target closed")

    _route_filter(_RacyRoute("document"))  # 예외가 새면 테스트 실패.


def _fake_playwright_module(monkeypatch, closed: dict):
    """new_context 가 실패하는 가짜 playwright 를 sys.modules 에 주입."""
    import sys
    import types

    class _B:
        def new_context(self, **kwargs):
            raise RuntimeError("boom — 기동 후 실패")

        def close(self) -> None:
            closed["browser"] = True

    class _PW:
        def __init__(self) -> None:
            self.chromium = types.SimpleNamespace(launch=lambda **kwargs: _B())

        def stop(self) -> None:
            closed["pw"] = True

    mod = types.SimpleNamespace(
        sync_playwright=lambda: types.SimpleNamespace(start=lambda: _PW())
    )
    monkeypatch.setitem(sys.modules, "playwright", types.SimpleNamespace(sync_api=mod))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)


def test_ensure_부분실패시_브라우저_정리(monkeypatch):
    """launch 성공 후 new_context 실패 — 브라우저 누수·래치 우회·영구 고장 방지(리뷰 HIGH-1)."""
    for cls in (PlaywrightRenderer, PlaywrightRender):
        closed = {"browser": False, "pw": False}
        _fake_playwright_module(monkeypatch, closed)
        r = cls()
        assert r._ensure() is False
        assert closed["browser"] and closed["pw"]  # 부분 상태가 정리됨(누수 방지).
        assert r._browser is None and r._context is None
        assert r._unavailable is True
        assert r._ensure() is False  # 래치가 우회되지 않는다(살아있다고 오판 금지).


def _wire(renderer, context) -> None:
    """내부 상태를 '기동됨'으로 주입(브라우저 없이 render 경로 진입)."""
    renderer._pw = _Closable()
    renderer._browser = _Closable()
    renderer._context = context


def test_enrich_렌더러_사망시_리셋():
    r = PlaywrightRenderer()
    _wire(r, _DeadContext())
    assert r.render("https://example.com") is None
    assert r._browser is None  # 리셋됨 — 다음 호출이 재기동.
    assert r._unavailable is False  # 재기동 가능해야 함(영구 비활성 아님).


def test_enrich_렌더러_일반실패는_유지():
    r = PlaywrightRenderer()
    ctx = _AliveContext()
    _wire(r, ctx)
    assert r.render("https://example.com") is None
    assert r._browser is not None  # 브라우저는 살아있음 — 리셋 안 함.


def test_existence_렌더러_사망시_리셋():
    r = PlaywrightRender()
    _wire(r, _DeadContext())
    assert r.render("example.com") is None
    assert r._browser is None
    assert r._unavailable is False


def test_existence_렌더러_일반실패는_유지():
    r = PlaywrightRender()
    _wire(r, _AliveContext())
    assert r.render("example.com") is None
    assert r._browser is not None
