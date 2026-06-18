"""Notion 자동 리포팅 (dry_run payload) 테스트."""

from __future__ import annotations

from leadcrawler.config import get_settings
from leadcrawler.integrations.notion import (
    DailyReport,
    NotionReporter,
    ScrumEntry,
    StatusTask,
)


def test_reporter_disabled_in_dry_run() -> None:
    assert NotionReporter(get_settings()).enabled is False


def test_daily_report_payload_shape() -> None:
    reporter = NotionReporter(get_settings())
    payload = reporter.post_daily_report(
        DailyReport(date="2026-06-18", milestone="M0", done="작업", next="다음")
    )
    assert payload["parent"]["database_id"] == get_settings().notion_daily_db
    assert payload["properties"]["제목"]["title"][0]["text"]["content"] == "2026-06-18 일일 보고"
    assert payload["properties"]["날짜"]["date"]["start"] == "2026-06-18"
    assert payload["properties"]["마일스톤"]["select"]["name"] == "M0"


def test_scrum_and_status_payloads() -> None:
    reporter = NotionReporter(get_settings())
    scrum = reporter.post_scrum(ScrumEntry(date="2026-06-18", today="할 일"))
    assert scrum["properties"]["오늘 할 일"]["rich_text"][0]["text"]["content"] == "할 일"
    status = reporter.post_status(StatusTask(task="T1", milestone="M0", status="진행중"))
    assert status["properties"]["상태"]["select"]["name"] == "진행중"
