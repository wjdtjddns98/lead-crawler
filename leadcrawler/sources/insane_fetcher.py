"""InsaneFetcher — 벤더링된 WAF우회 엔진을 :class:`SupportsFetch` 로 감싸는 어댑터(A3).

정적 HTTP 가 WAF(Incapsula/Cloudflare 등)로 막힌 발견 소스(SET/Bursa 등)가 일반 페처 대신
주입해 쓰는 어댑터. 엔진의 ``fetch(url) -> FetchResult{ok, content}`` 를 호출해 ok 면 HTML
을 돌려주고, 실패(ok=False)·미설치·오류는 **graceful 하게 빈 문자열**로 흡수한다(파이프라인
무중단). 엔진은 GET-HTML 우회 전용이라 POST 계열은 미지원(NotImplementedError).

dry_run 분기는 호출부(소스의 ``discover``)가 책임진다 — dry_run 이면 ``_live`` 자체가 안 불려
이 어댑터는 네트워크를 타지 않는다. 그래도 방어적으로, fetch_fn 미주입 + 엔진 미설치면 모든
호출이 빈 결과가 된다(no-op).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from ..logging import get_logger

log = get_logger("sources.insane")

# 엔진 fetch 시그니처(부분): fetch(url, *, timeout, max_attempts, enable_playwright) -> result.
FetchFn = Callable[..., Any]


def _default_fetch_fn() -> FetchFn | None:
    """벤더링된 엔진의 ``fetch`` 를 지연 임포트한다(curl_cffi 등 미설치면 None)."""
    try:
        from ._bypass.engine import fetch  # 벤더(MIT) — insane_fetcher 만 호출

        return fetch
    except Exception as exc:  # ImportError(curl_cffi 등)·기타 → 미가용.
        log.info("insane.engine_unavailable", err=str(exc))
        return None


class InsaneFetcher:
    """WAF우회 엔진 기반 :class:`SupportsFetch` — ok 면 HTML, 그 외 graceful 빈 결과.

    엔진 미설치/오류/ok=False 는 빈 문자열로 흡수한다(소스는 빈 HTML → 빈 결과로 graceful).
    POST 계열은 우회 범위 밖이라 :class:`NotImplementedError`.
    """

    def __init__(
        self,
        *,
        fetch_fn: FetchFn | None = None,
        timeout: float = 25.0,
        enable_playwright: bool = False,
        max_attempts: int = 12,
    ) -> None:
        # 명시 주입(테스트) > 엔진 지연임포트. 둘 다 없으면 모든 호출 빈 결과(no-op).
        self._fetch_fn = fetch_fn if fetch_fn is not None else _default_fetch_fn()
        self._timeout = timeout
        # 임베드에선 playwright_mcp 티어가 미동작이라 기본 off(curl_cffi 격자만). 로컬 Playwright
        # 폴백이 필요하면 True(엔진이 graceful 처리). A5 한계 참조.
        self._enable_playwright = enable_playwright
        self._max_attempts = max_attempts

    @property
    def available(self) -> bool:
        """우회 엔진(또는 주입된 fetch_fn)이 가용한지."""
        return self._fetch_fn is not None

    def _full_url(self, url: str, params: dict[str, Any] | None) -> str:
        if not params:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{urlencode(params)}"

    def get_text(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,  # 엔진이 프로파일 헤더를 관리 — 무시.
    ) -> str:
        """우회 GET 후 HTML 본문을 반환한다(실패·미가용 시 빈 문자열, graceful)."""
        if self._fetch_fn is None:
            return ""
        full = self._full_url(url, params)
        try:
            result = self._fetch_fn(
                full,
                timeout=self._timeout,
                max_attempts=self._max_attempts,
                enable_playwright=self._enable_playwright,
            )
        except Exception as exc:  # 엔진 내부 예외 → graceful 빈 결과.
            log.info("insane.fetch_error", url=url, err=str(exc))
            return ""
        if not getattr(result, "ok", False):
            log.info("insane.fetch_blocked", url=url)
            return ""
        return getattr(result, "content", "") or ""

    def get_bytes(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """우회 GET 본문 바이트(HTML 을 UTF-8 인코딩)."""
        return self.get_text(url, params=params, headers=headers).encode("utf-8")

    def get_json(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """우회 GET 후 JSON 파싱(빈/비 JSON 응답은 예외 → 호출부가 graceful 처리)."""
        text = self.get_text(url, params=params, headers=headers)
        if not text:
            raise RuntimeError("insane: empty/blocked response (no JSON)")
        return json.loads(text)

    def post_text(self, url: str, **_: Any) -> str:
        raise NotImplementedError("InsaneFetcher 는 GET-HTML 우회 전용(POST 미지원)")

    def post_json(self, url: str, **_: Any) -> Any:
        raise NotImplementedError("InsaneFetcher 는 GET-HTML 우회 전용(POST 미지원)")
