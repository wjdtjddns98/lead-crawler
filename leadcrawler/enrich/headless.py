"""헤드리스 렌더링 — 정적 추출로 이메일을 못 찾은 기업의 JS 렌더 페이지 보강.

Playwright 는 **선택적 의존성**(extra ``crawl`` — ``pip install lead-crawler[crawl]`` +
``playwright install chromium``)이다. 미설치/실패해도 렌더러는
``None`` 을 돌려줄 뿐 파이프라인을 깨지 않는다(보강은 정적 결과로 폴백). dry_run 은
이 경로를 타지 않으며(결정적), 라이브에서도 ``enrich_headless`` 를 켤 때만 동작한다.

테스트는 :class:`SupportsRender` 를 구현한 가짜 렌더러를 주입해 브라우저·네트워크 없이
escalation 로직을 검증한다.
"""

from __future__ import annotations

from typing import Protocol

from ..logging import get_logger

log = get_logger("enrich.headless")


class SupportsRender(Protocol):
    """헤드리스 렌더러 인터페이스(테스트 더블이 구현)."""

    def render(self, url: str) -> str | None:
        """페이지를 렌더해 최종 HTML 을 반환(실패 시 None)."""
        ...

    def close(self) -> None:
        """브라우저/리소스를 정리한다."""
        ...


class PlaywrightRenderer:
    """Playwright(Chromium) 기반 실 렌더러 — lazy 기동·재사용·graceful 실패."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._timeout = timeout
        self._pw = None
        self._browser = None
        self._unavailable = False  # 미설치/기동실패 시 재시도 안 함.

    def _ensure(self) -> bool:
        """브라우저를 1회 기동(재사용). 미설치/실패면 False(이후 비활성)."""
        if self._browser is not None:
            return True
        if self._unavailable:
            return False
        try:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            return True
        except Exception as exc:  # 미설치(ImportError)·브라우저 미설치·기동실패.
            log.info("headless.unavailable", err=str(exc))
            self._unavailable = True
            return False

    def render(self, url: str) -> str | None:
        """URL 을 렌더해 DOM 안정화 후 HTML 을 반환한다(실패 시 None)."""
        if not self._ensure():
            return None
        page = None
        try:
            page = self._browser.new_page()
            page.goto(url, timeout=int(self._timeout * 1000), wait_until="networkidle")
            return page.content()
        except Exception as exc:  # 타임아웃·네비게이션 실패 → 건너뛴다.
            log.info("headless.render.error", url=url, err=str(exc))
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: S110 — 정리 실패는 무시.
                    pass

    def close(self) -> None:
        """브라우저·Playwright 를 정리한다(커넥션 누수 방지)."""
        for obj, method in ((self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:  # noqa: S110 — 정리 실패는 무시.
                    pass
        self._browser = None
        self._pw = None
