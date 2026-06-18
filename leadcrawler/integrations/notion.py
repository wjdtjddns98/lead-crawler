"""Notion 자동 리포팅 — 일일보고서·데일리스크럼·현황 보드 자동 기입.

PO 요청: Notion 운영 서식은 **사람이 직접 작성하지 않는다**. 크롤러 통계와 git
활동을 모아 이 모듈이 매일 자동으로 행을 추가/갱신한다.

``dry_run`` 이거나 ``notion_token`` 이 없으면 네트워크 호출 없이, 보낼 payload 를
그대로 반환한다(결정적 — 테스트가 네트워크 없이 검증 가능).
"""

from __future__ import annotations

from pydantic import BaseModel

from ..config import Settings, get_settings
from ..logging import get_logger

log = get_logger("notion")

_API = "https://api.notion.com/v1/pages"


class DailyReport(BaseModel):
    """일일 보고서 한 건."""

    date: str  # YYYY-MM-DD
    author: str = "시스템(자동)"
    milestone: str | None = None
    done: str = ""
    next: str = ""
    issues: str = "없음"
    status: str = "정상"


class ScrumEntry(BaseModel):
    """데일리 스크럼 한 건."""

    date: str
    author: str = "시스템(자동)"
    yesterday: str = ""
    today: str = ""
    blocker: str = "없음"


class StatusTask(BaseModel):
    """현황 보드 태스크 한 건."""

    task: str
    milestone: str | None = None
    status: str = "Todo"
    priority: str = "Mid"
    owner: str = ""
    note: str = ""


def _text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value}}]} if value else {"rich_text": []}


def _title(value: str) -> dict:
    return {"title": [{"text": {"content": value}}]}


def _select(value: str | None) -> dict:
    return {"select": {"name": value}} if value else {"select": None}


def _date(value: str) -> dict:
    return {"date": {"start": value}}


class NotionReporter:
    """Notion DB 에 보고/스크럼/현황을 자동 기입한다."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        """실제 전송 가능 여부(dry_run 아님 + 토큰 존재)."""
        return not self.settings.dry_run and bool(self.settings.notion_token)

    def daily_report_payload(self, report: DailyReport) -> dict:
        """일일 보고서 생성 payload 를 만든다."""
        return {
            "parent": {"database_id": self.settings.notion_daily_db},
            "properties": {
                "제목": _title(f"{report.date} 일일 보고"),
                "날짜": _date(report.date),
                "작성자": _text(report.author),
                "마일스톤": _select(report.milestone),
                "한 일": _text(report.done),
                "내일 할 일": _text(report.next),
                "이슈/블로커": _text(report.issues),
                "진행 상태": _select(report.status),
            },
        }

    def scrum_payload(self, entry: ScrumEntry) -> dict:
        """데일리 스크럼 생성 payload 를 만든다."""
        return {
            "parent": {"database_id": self.settings.notion_scrum_db},
            "properties": {
                "제목": _title(f"{entry.date} 스크럼"),
                "날짜": _date(entry.date),
                "작성자": _text(entry.author),
                "어제 한 일": _text(entry.yesterday),
                "오늘 할 일": _text(entry.today),
                "블로커": _text(entry.blocker),
            },
        }

    def status_payload(self, task: StatusTask) -> dict:
        """현황 보드 태스크 생성 payload 를 만든다."""
        return {
            "parent": {"database_id": self.settings.notion_status_db},
            "properties": {
                "태스크": _title(task.task),
                "마일스톤": _select(task.milestone),
                "상태": _select(task.status),
                "우선순위": _select(task.priority),
                "담당": _text(task.owner),
                "비고": _text(task.note),
            },
        }

    def _post(self, payload: dict, *, what: str) -> dict:
        """payload 를 Notion 에 전송한다. 비활성 시 네트워크 없이 payload 반환."""
        if not self.enabled:
            log.info("notion.dry_run", what=what, db=payload["parent"]["database_id"])
            return payload
        import httpx

        headers = {
            "Authorization": f"Bearer {self.settings.notion_token}",
            "Notion-Version": self.settings.notion_version,
            "Content-Type": "application/json",
        }
        resp = httpx.post(_API, json=payload, headers=headers, timeout=30.0)
        if resp.status_code >= 400:
            raise RuntimeError(f"notion {what} 전송 실패: HTTP {resp.status_code}")
        log.info("notion.posted", what=what)
        return resp.json()

    def post_daily_report(self, report: DailyReport) -> dict:
        """일일 보고서 1건을 기입한다."""
        return self._post(self.daily_report_payload(report), what="daily_report")

    def post_scrum(self, entry: ScrumEntry) -> dict:
        """데일리 스크럼 1건을 기입한다."""
        return self._post(self.scrum_payload(entry), what="scrum")

    def post_status(self, task: StatusTask) -> dict:
        """현황 보드 태스크 1건을 기입한다."""
        return self._post(self.status_payload(task), what="status")
