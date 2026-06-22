"""스케줄러 테스트 — 일일 잡 본체(dry_run) + opt-in/extra graceful 게이트."""

from __future__ import annotations

import pytest

from leadcrawler.config import Settings
from leadcrawler.scheduler import run_daily_report, start_scheduler


def test_run_daily_report_dry_run() -> None:
    """dry_run 기본 — 네트워크 없이 크롤 1회전 후 3종 보드 payload 반환."""
    settings = Settings(report_countries="KR", report_industries="건설", report_milestone="M3")
    result = run_daily_report(settings, date="2026-06-22")
    assert set(result) == {"daily", "scrum", "status"}
    assert result["daily"]["properties"]["날짜"]["date"]["start"] == "2026-06-22"


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
