"""run.drain_completed — 완료 순 소비·진행 신호·멈춘 항목 격리(pool.map 순서 소비 대체)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from leadcrawler.pipeline.run import drain_completed


def test_slow_first_item_does_not_block_progress_beats() -> None:
    """앞 항목이 느려도 뒤 항목 완료마다 beat·handle 이 즉시 일어난다(head-of-line 차단 없음)."""
    release = threading.Event()
    beats: list[float] = []
    handled: list[str] = []

    def work(item: str) -> str:
        if item == "slow":
            release.wait(5)
        return item

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        # 느린 항목을 0.5s 뒤 풀어주는 타이머를 두고 idle_timeout 없이 전부 완료를 기다린다.
        threading.Timer(0.5, release.set).start()
        stuck = drain_completed(
            pool, work, ["slow", "a", "b", "c"],
            handle=handled.append, beat=lambda: beats.append(time.monotonic() - t0), idle_timeout=None,
        )
    assert stuck == []
    assert sorted(handled) == ["a", "b", "c", "slow"]
    # a/b/c 는 slow(0.5s 뒤 해제)보다 먼저 처리됐다 — pool.map 이었다면 전부 0.5s 이후.
    assert min(beats[:3]) < 0.4


def test_idle_timeout_isolates_stuck_items_and_counts_them() -> None:
    """완료 간격이 idle_timeout 을 넘으면 남은 항목을 '멈춤'으로 돌려주고 소비를 끝낸다."""
    release = threading.Event()
    handled: list[str] = []
    logged: list[list[str]] = []

    def work(item: str) -> str:
        if item.startswith("hang"):
            release.wait(10)
        return item

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            stuck = drain_completed(
                pool, work, ["ok1", "hang1", "hang2", "ok2"],
                handle=handled.append, idle_timeout=0.5, log_stuck=logged.append,
            )
            release.set()  # 풀 종료 대기가 막히지 않게 테스트에서 풀어준다.
    finally:
        release.set()
    assert set(handled) >= {"ok1"}
    assert set(stuck) >= {"hang1"} and all(s.startswith("hang") or s == "ok2" for s in stuck)
    assert logged and logged[0] == stuck


def test_no_items_returns_empty() -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert drain_completed(pool, lambda x: x, [], handle=lambda r: None) == []
