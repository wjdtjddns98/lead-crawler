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


def test_nutti_daily_payload_shape() -> None:
    reporter = NotionReporter(get_settings())
    payload = reporter.post_nutti_daily(
        DailyReport(date="2026-07-21", done="줄1\n- 줄2", next="다음 계획")
    )
    assert payload["database_id"] == get_settings().notion_nutti_daily_db
    assert payload["title"] == "Daily Report 07.21"
    kids = payload["children"]
    assert kids[0]["heading_3"]["rich_text"][0]["text"]["content"] == "lead-crawler(자동)"
    texts = [b["bulleted_list_item"]["rich_text"][0]["text"]["content"] for b in kids[1:]]
    assert "줄1" in texts
    assert "줄2" in texts  # '- ' 프리픽스 제거 확인
    assert "내일: 다음 계획" in texts
    assert texts[-1].startswith("이슈: ")
