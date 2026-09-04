"""스케줄러 테스트 — 일일 잡 본체(dry_run) + opt-in/extra graceful 게이트."""

from __future__ import annotations

import pytest

import leadcrawler.scheduler as sched
from leadcrawler.config import Settings, get_settings
from leadcrawler.scheduler import run_daily_report, start_scheduler
from leadcrawler.storage.crawl_job import create_crawl_job
from leadcrawler.storage.db import init_db, session_scope


def test_run_daily_report_dry_run() -> None:
    """dry_run 기본 — 네트워크 없이 크롤 1회전 후 3종 보드 payload 반환."""
    settings = Settings(report_countries="KR", report_industries="건설", report_milestone="M3")
    result = run_daily_report(settings, date="2026-06-22")
    assert set(result) == {"daily", "scrum", "status"}
    assert result["daily"]["properties"]["날짜"]["date"]["start"] == "2026-06-22"


def test_run_daily_report_skips_crawl_when_continuous_running(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """웹 연속 크롤 잡이 running 이면 데일리 크롤은 스킵(이중 크롤 방지), 리포팅은 수행."""
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/sched.db")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as db:
        create_crawl_job(
            db, countries="", industries="건설", listed="unknown",
            persist=False, segments_total=1, triggered_by="x", mode="continuous",
        )
    called = {"v": False}
    monkeypatch.setattr(sched, "run_pipeline", lambda *_a, **_k: called.update(v=True) or [])
    result = run_daily_report(settings, date="2026-07-02")
    assert called["v"] is False  # 크롤 안 돎.
    assert set(result) == {"daily", "scrum", "status"}  # 리포팅은 그대로.


def test_start_scheduler_disabled_by_default() -> None:
    """scheduler_enabled=False(기본)면 기동 거부 — 우발 기동 방지."""
    with pytest.raises(RuntimeError, match="비활성"):
        start_scheduler(Settings())


def test_start_scheduler_missing_apscheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """APScheduler 미설치면 설치 안내 RuntimeError(우발 기동 0)."""
    import builtins

    real_import = builtins.__import__

    def _no_apscheduler(name: str, *a: object, **k: object) -> object:
        if name.startswith("apscheduler"):
            raise ModuleNotFoundError("No module named 'apscheduler'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_apscheduler)
    with pytest.raises(RuntimeError, match="APScheduler"):
        start_scheduler(Settings(scheduler_enabled=True))
