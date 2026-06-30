"""공유 호스트별 레이트리미터 — 직렬 슬롯 예약·호스트 캐시·Fetcher 페이싱(네트워크 없음).

전부 인메모리/MockTransport 라 네트워크를 타지 않는다. 타이밍 단언은 하한(>=)만 보고
상한은 보지 않아(슬랙) CI 부하에도 flaky 하지 않게 한다.
"""

from __future__ import annotations

import threading
import time

import httpx

from leadcrawler.sources.http import Fetcher, HostRateLimiters, RateLimiter


def test_rate_limiter_serializes_acquires() -> None:
    """N회 acquire 는 최소 (N-1)*interval 만큼 걸린다(직렬 슬롯 예약)."""
    rate = 20.0  # interval = 0.05s
    interval = 1.0 / rate
    n = 5
    rl = RateLimiter(rate_per_sec=rate)
    start = time.monotonic()
    for _ in range(n):
        rl.acquire()
    elapsed = time.monotonic() - start
    # 첫 슬롯은 즉시, 이후 (N-1) 슬롯은 interval 간격 → 하한 (N-1)*interval(슬랙 0.8).
    assert elapsed >= (n - 1) * interval * 0.8


def test_rate_limiter_zero_is_noop() -> None:
    """rate<=0 이면 대기 없이 즉시 반환(무제한)."""
    rl = RateLimiter(rate_per_sec=0)
    start = time.monotonic()
    for _ in range(200):
        rl.acquire()
    assert time.monotonic() - start < 0.05


def test_rate_limiter_serializes_across_threads() -> None:
    """여러 스레드에서 동시 acquire 해도 총 발사가 직렬화돼 초당 rate 를 넘지 않는다."""
    rate = 50.0  # interval = 0.02s
    interval = 1.0 / rate
    rl = RateLimiter(rate_per_sec=rate)
    threads_n, per_thread = 4, 5
    total = threads_n * per_thread

    def worker() -> None:
        for _ in range(per_thread):
            rl.acquire()

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start
    # 총 total 슬롯이 직렬 예약 → 하한 (total-1)*interval(슬랙 0.7 — 스케줄 지터 흡수).
    assert elapsed >= (total - 1) * interval * 0.7


def test_host_rate_limiters_caches_per_host() -> None:
    """같은 host 는 동일 인스턴스, 다른 host 는 다른 인스턴스(공유 한도 = host 단위)."""
    hrl = HostRateLimiters(default_rate=5.0)
    a = hrl.for_host("data.sec.gov")
    b = hrl.for_host("data.sec.gov")
    c = hrl.for_host("api.gleif.org")
    assert a is b  # 같은 host → 같은 limiter(공유).
    assert a is not c  # 다른 host → 별도 limiter.


def test_host_rate_limiters_per_host_override() -> None:
    """per_host 오버라이드가 있으면 그 host 만 별도 rate(없으면 default)."""
    hrl = HostRateLimiters(default_rate=0.0, per_host={"slow.example": 1000.0})
    # default_rate=0 → no-op(즉시), 오버라이드 host 는 유한 interval.
    assert hrl.for_host("any.example")._min_interval == 0.0
    assert hrl.for_host("slow.example")._min_interval == 1.0 / 1000.0


class _SpyLimiter:
    def __init__(self, events: list) -> None:
        self._events = events

    def acquire(self) -> None:
        self._events.append(("acquire", None))


class _SpyLimiters:
    """HostRateLimiters 스파이 — for_host/acquire 호출과 host 인자를 기록."""

    def __init__(self, events: list) -> None:
        self._events = events
        self.hosts: list[str] = []

    def for_host(self, host: str):
        self.hosts.append(host)
        return _SpyLimiter(self._events)


def test_fetcher_paces_before_request() -> None:
    """rate_limiters 주입 시 요청 직전 host limiter.acquire() 가 먼저 불린다."""
    events: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(("request", request.url.host))
        return httpx.Response(200, json={"ok": True})

    spy = _SpyLimiters(events)
    fetcher = Fetcher(min_interval=0.0, rate_limiters=spy)
    fetcher._client = httpx.Client(transport=httpx.MockTransport(handler))

    fetcher.get_json("https://data.sec.gov/submissions/CIK1.json")
    # acquire 가 request 보다 먼저, host 는 URL hostname.
    assert events == [("acquire", None), ("request", "data.sec.gov")]
    assert spy.hosts == ["data.sec.gov"]


def test_fetcher_without_limiters_is_unpaced() -> None:
    """rate_limiters=None(기본) 이면 limiter 를 전혀 부르지 않는다(회귀 0)."""
    events: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(("request", request.url.host))
        return httpx.Response(200, json={"ok": True})

    fetcher = Fetcher(min_interval=0.0)  # rate_limiters 미주입.
    fetcher._client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher.get_json("https://example.com/x")
    assert events == [("request", "example.com")]  # acquire 없음.
