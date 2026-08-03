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
    assert not browser_dead(TimeoutError("Timeout 20000ms exceeded"))


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
