"""Notion 자동 리포팅 (dry_run payload + Nutti 병기 분기) 테스트."""

from __future__ import annotations

import pytest

from leadcrawler.config import Settings, get_settings
from leadcrawler.integrations.notion import (
    DailyReport,
    NotionReporter,
    ScrumEntry,
    StatusTask,
)


class _Resp:
    """httpx 응답 스텁 — status_code/json()/text 만 흉내낸다."""

    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self) -> dict:
        return self._payload


def _live_reporter(db: str = "db1") -> NotionReporter:
    return NotionReporter(
        Settings(dry_run=False, notion_token="tkn", notion_nutti_daily_db=db)
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
    reporter = NotionReporter(Settings(notion_nutti_daily_db="testdb"))  # dry_run 기본 true
    payload = reporter.post_nutti_daily(
        DailyReport(date="2026-07-21", done="줄1\n- 줄2", next="다음 계획")
    )
    assert payload["database_id"] == "testdb"
    assert payload["title"] == "Daily Report 07.21"
    kids = payload["children"]
    assert kids[0]["heading_3"]["rich_text"][0]["text"]["content"] == "lead-crawler(자동)"
    texts = [b["bulleted_list_item"]["rich_text"][0]["text"]["content"] for b in kids[1:]]
    assert "줄1" in texts
    assert "줄2" in texts  # '- ' 프리픽스 제거 확인
    assert "내일: 다음 계획" in texts
    assert texts[-1].startswith("이슈: ")


def test_nutti_blocks_clamped_to_notion_limits() -> None:
    reporter = NotionReporter(Settings(notion_nutti_daily_db="testdb"))
    report = DailyReport(date="2026-07-21", done="\n".join(f"줄{i}" for i in range(150)))
    blocks = reporter.nutti_daily_blocks(report)
    assert len(blocks) == 100  # children 한도 클램프
    assert "생략" in blocks[-1]["bulleted_list_item"]["rich_text"][0]["text"]["content"]
    long_report = DailyReport(date="2026-07-21", done="가" * 3000)
    long_blocks = reporter.nutti_daily_blocks(long_report)
    assert len(long_blocks[1]["bulleted_list_item"]["rich_text"][0]["text"]["content"]) == 2000


def test_nutti_live_appends_when_page_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kw: _Resp(payload={"results": [{"id": "page1"}]}),
    )
    monkeypatch.setattr(
        "httpx.get", lambda url, **kw: _Resp(payload={"results": [], "has_more": False})
    )

    def fake_patch(url, **kw):
        calls["patch"] = (url, kw["json"])
        return _Resp(payload={"object": "list"})

    monkeypatch.setattr("httpx.patch", fake_patch)
    out = _live_reporter().post_nutti_daily(DailyReport(date="2026-07-21", done="한 일"))
    assert out == {"object": "list"}
    assert "blocks/page1/children" in calls["patch"][0]
    assert calls["patch"][1]["children"][0]["type"] == "heading_3"


def test_nutti_live_skips_when_section_already_posted(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = {
        "results": [
            {
                "type": "heading_3",
                "heading_3": {"rich_text": [{"plain_text": "lead-crawler(자동)"}]},
            }
        ],
        "has_more": False,
    }
    monkeypatch.setattr(
        "httpx.post", lambda url, **kw: _Resp(payload={"results": [{"id": "page1"}]})
    )
    monkeypatch.setattr("httpx.get", lambda url, **kw: _Resp(payload=existing))
    monkeypatch.setattr(
        "httpx.patch", lambda url, **kw: pytest.fail("중복 섹션인데 PATCH 호출됨")
    )
    out = _live_reporter().post_nutti_daily(DailyReport(date="2026-07-21", done="한 일"))
    assert out == {"skipped": "already_posted", "page_id": "page1"}


def test_nutti_live_creates_page_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []

    def fake_post(url, **kw):
        calls.append((url, kw["json"]))
        if url.endswith("/query"):
            return _Resp(payload={"results": []})
        return _Resp(payload={"id": "new-page"})

    monkeypatch.setattr("httpx.post", fake_post)
    out = _live_reporter().post_nutti_daily(DailyReport(date="2026-07-21", done="한 일"))
    assert out == {"id": "new-page"}
    create_url, create_json = calls[-1]
    assert create_url.endswith("/pages")
    props = create_json["properties"]
    assert props["제목"]["title"][0]["text"]["content"] == "Daily Report 07.21"
    assert props["작성일"]["date"]["start"] == "2026-07-21"
    assert props["Tag"]["multi_select"] == [{"name": "업무보고"}]


def test_nutti_live_query_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("httpx.post", lambda url, **kw: _Resp(status_code=404))
    with pytest.raises(RuntimeError, match="조회 실패"):
        _live_reporter().post_nutti_daily(DailyReport(date="2026-07-21", done="한 일"))


def test_auto_report_nutti_failure_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    from leadcrawler.reporting import auto_report

    def boom(self, report):
        raise RuntimeError("boom")

    monkeypatch.setattr(NotionReporter, "post_nutti_daily", boom)
    out = auto_report(
        [], date="2026-07-21", settings=Settings(notion_nutti_daily_db="db1"), commits=[]
    )
    assert set(out) == {"daily", "scrum", "status"}  # 3종은 그대로, nutti 만 빠짐


def test_auto_report_nutti_off_by_default() -> None:
    from leadcrawler.reporting import auto_report

    out = auto_report([], date="2026-07-21", settings=Settings(), commits=[])
    assert "nutti" not in out  # 기본값(빈 DB id)=옵트인 꺼짐
