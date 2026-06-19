"""발견 소스 공용 HTTP 페처 — 주입 가능(테스트는 가짜 페처로 모킹).

라이브 소스는 :class:`Fetcher` 를 통해서만 네트워크에 접근한다. 이로써:
- 단위 테스트는 :class:`SupportsFetch` 를 구현한 가짜 객체로 파싱 로직을 네트워크 없이 검증,
- 레이트리밋(요청 간 최소 간격)·재시도(tenacity)·공통 헤더(UA)를 한 곳에서 강제.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..logging import get_logger

log = get_logger("sources.http")

# 일시적 네트워크 오류 + 재시도 가치 있는 상태코드(429/5xx)만 재시도. 그 외 4xx 는 즉시 실패.
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRY_STATUS
    return False


@runtime_checkable
class SupportsFetch(Protocol):
    """라이브 소스가 의존하는 최소 HTTP 인터페이스(테스트 더블이 구현)."""

    def get_json(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        ...

    def get_bytes(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        ...

    def get_text(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        ...

    def post_text(
        self, url: str, *, data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
    ) -> str:
        ...


class Fetcher:
    """httpx 기반 실 페처 — 레이트리밋 + 재시도 + 공통 UA 헤더."""

    def __init__(
        self,
        *,
        user_agent: str = "",
        min_interval: float = 0.12,
        timeout: float = 15.0,
    ) -> None:
        self._min_interval = min_interval
        self._timeout = timeout
        self._last = 0.0
        headers = {"User-Agent": user_agent} if user_agent else {}
        self._client = httpx.Client(timeout=timeout, headers=headers, follow_redirects=True)

    def _throttle(self) -> None:
        """직전 요청과의 간격이 ``min_interval`` 미만이면 대기한다."""
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=0.5, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _get(
        self, url: str, params: dict[str, Any] | None, headers: dict[str, str] | None
    ) -> httpx.Response:
        self._throttle()
        resp = self._client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=0.5, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _post(
        self, url: str, data: dict[str, Any] | None,
        params: dict[str, Any] | None, headers: dict[str, str] | None,
    ) -> httpx.Response:
        self._throttle()
        resp = self._client.post(url, data=data, params=params, headers=headers)
        resp.raise_for_status()
        return resp

    def get_json(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET 후 JSON 파싱 결과를 반환한다."""
        return self._get(url, params, headers).json()

    def get_bytes(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """GET 후 본문 바이트를 반환한다(예: ZIP)."""
        return self._get(url, params, headers).content

    def get_text(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """GET 후 본문 텍스트(HTML)를 반환한다."""
        return self._get(url, params, headers).text

    def post_text(
        self, url: str, *, data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
    ) -> str:
        """POST(폼) 후 본문 텍스트(HTML)를 반환한다(예: PSE 페이지네이션)."""
        return self._post(url, data, params, headers).text

    def close(self) -> None:
        self._client.close()
