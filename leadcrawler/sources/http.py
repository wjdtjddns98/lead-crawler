"""발견 소스 공용 HTTP 페처 — 주입 가능(테스트는 가짜 페처로 모킹).

라이브 소스는 :class:`Fetcher` 를 통해서만 네트워크에 접근한다. 이로써:
- 단위 테스트는 :class:`SupportsFetch` 를 구현한 가짜 객체로 파싱 로직을 네트워크 없이 검증,
- 레이트리밋(요청 간 최소 간격)·재시도(tenacity)·공통 헤더(UA)를 한 곳에서 강제.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..logging import get_logger

log = get_logger("sources.http")

# 일시적 네트워크 오류 + 재시도 가치 있는 상태코드(429/5xx)만 재시도. 그 외 4xx 는 즉시 실패.
_RETRY_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """호스트 단위 토큰 스로틀 — '다음 슬롯 예약' 방식으로 초당 ``rate`` 를 직렬 보장한다.

    동시 호출이 와도 lock 안에서 다음 가용 슬롯(``self._next``)을 순차 예약하고, 실제 대기
    (``sleep``)는 lock 밖에서 한다 — 대기 중 다른 스레드의 슬롯 예약이 막히지 않게(스로틀러
    자체가 병목이 되지 않게). 이로써 동시 호출의 총 발사율이 초당 ``rate`` 를 넘지 않는다.
    ``rate_per_sec <= 0`` 이면 no-op(무제한).
    """

    def __init__(self, rate_per_sec: float) -> None:
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._next = 0.0  # 다음 슬롯이 열리는 monotonic 시각.
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """다음 슬롯을 예약하고(lock 안) 그 시각까지 대기(lock 밖)한다."""
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)  # 슬롯은 지금 또는 직전 예약 이후로만 열린다.
            self._next = start + self._min_interval
            wait = start - now
        if wait > 0:
            time.sleep(wait)


class HostRateLimiters:
    """호스트별 :class:`RateLimiter` 레지스트리 — 여러 페처가 같은 호스트 한도를 공유한다.

    세그먼트 병렬화 시 워커별 독립 Fetcher 들이 같은 등록처 호스트(``data.sec.gov`` 등)를
    동시에 때려도, 이 공유 레지스트리의 호스트별 limiter 로 합산 초당 요청을 ``default_rate``
    (또는 ``per_host`` 오버라이드) 이하로 묶어 429 를 선제 방지한다. 스레드 안전(dict 캐시
    접근을 lock 으로 보호).
    """

    def __init__(self, default_rate: float, per_host: dict[str, float] | None = None) -> None:
        self._default_rate = default_rate
        self._per_host = dict(per_host) if per_host else {}
        self._limiters: dict[str, RateLimiter] = {}
        self._lock = threading.Lock()

    def for_host(self, host: str) -> RateLimiter:
        """``host`` 의 공유 limiter 를 반환한다(없으면 rate 로 생성·캐시)."""
        with self._lock:
            limiter = self._limiters.get(host)
            if limiter is None:
                limiter = RateLimiter(self._per_host.get(host, self._default_rate))
                self._limiters[host] = limiter
            return limiter


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

    def post_json(
        self, url: str, *, json: Any | None = None,
        params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
    ) -> Any:
        ...


class Fetcher:
    """httpx 기반 실 페처 — 레이트리밋 + 재시도 + 공통 UA 헤더."""

    def __init__(
        self,
        *,
        user_agent: str = "",
        min_interval: float = 0.12,
        timeout: float = 15.0,
        rate_limiters: HostRateLimiters | None = None,
    ) -> None:
        self._min_interval = min_interval
        self._timeout = timeout
        self._last = 0.0
        # 공유 호스트별 레이트리미터(opt-in). 주입되면 요청 직전 호스트 슬롯을 예약해 여러
        # 페처(워커별)의 합산 초당 요청을 호스트 한도 이하로 묶는다. None 이면 기존 동작(회귀 0).
        self._rate_limiters = rate_limiters
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

    def _pace(self, url: str) -> None:
        """요청 직전 페이싱 — 공유 호스트 레이트리미터(있으면) + 인스턴스 throttle 을 적용한다.

        ``rate_limiters`` 가 주입되면 URL 호스트별 공유 슬롯을 먼저 예약(429 선제 방지)한
        뒤 기존 인스턴스 단위 ``min_interval`` throttle 을 적용한다. None 이면 throttle 만 —
        기존 동작 그대로(회귀 0).
        """
        if self._rate_limiters is not None:
            host = urlparse(url).hostname
            if host:
                self._rate_limiters.for_host(host).acquire()
        self._throttle()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=0.5, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _get(
        self, url: str, params: dict[str, Any] | None, headers: dict[str, str] | None
    ) -> httpx.Response:
        self._pace(url)
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
        self, url: str, data: dict[str, Any] | None, json: Any | None,
        params: dict[str, Any] | None, headers: dict[str, str] | None,
    ) -> httpx.Response:
        self._pace(url)
        resp = self._client.post(url, data=data, json=json, params=params, headers=headers)
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
        return self._post(url, data, None, params, headers).text

    def post_json(
        self, url: str, *, json: Any | None = None,
        params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
    ) -> Any:
        """POST(JSON 본문) 후 JSON 파싱 결과를 반환한다(예: Apollo people search)."""
        return self._post(url, None, json, params, headers).json()

    def close(self) -> None:
        self._client.close()
