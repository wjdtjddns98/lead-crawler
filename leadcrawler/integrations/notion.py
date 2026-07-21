"""Notion 자동 리포팅 — 일일보고서·데일리스크럼·현황 보드 자동 기입.

PO 요청: Notion 운영 서식은 **사람이 직접 작성하지 않는다**. 크롤러 통계와 git
활동을 모아 이 모듈이 매일 자동으로 행을 추가/갱신한다.

``dry_run`` 이거나 ``notion_token`` 이 없으면 네트워크 호출 없이, 보낼 payload 를
그대로 반환한다(결정적 — 테스트가 네트워크 없이 검증 가능).
"""

from __future__ import annotations

from datetime import date as _iso_date

from pydantic import BaseModel

from ..config import Settings, get_settings
from ..logging import get_logger

log = get_logger("notion")

_API = "https://api.notion.com/v1/pages"
_QUERY_API = "https://api.notion.com/v1/databases/{db}/query"
_BLOCKS_API = "https://api.notion.com/v1/blocks/{page_id}/children"
_NUTTI_SECTION = "lead-crawler(자동)"  # Nutti 페이지 내 우리 섹션 헤딩(중복 기입 판별 키)
_MAX_CHILDREN = 100  # Notion API: 요청당 children 블록 한도
_MAX_RICH_TEXT = 2000  # Notion API: rich_text content 길이 한도


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

    # ── Nutti 팀 일일 업무보고 병기 ──────────────────────────────────────────
    # Nutti 서식은 "하루 한 페이지(Daily Report MM.DD) + 사람별 ### 섹션" 구조라,
    # 행 추가가 아니라 그날 페이지 본문에 lead-crawler 섹션 블록을 append 한다.

    @staticmethod
    def _nutti_page_title(date: str) -> str:
        """Nutti 서식 관례 제목 — 'Daily Report MM.DD'(비ISO 날짜는 명시적 에러)."""
        return f"Daily Report {_iso_date.fromisoformat(date).strftime('%m.%d')}"

    def nutti_daily_blocks(self, report: DailyReport) -> list[dict]:
        """그날 Nutti 페이지에 붙일 lead-crawler 섹션 블록 목록(결정적).

        Notion 한도 방어: 각 텍스트는 2000자 절단, 블록 수는 100개로 클램프(초과분은
        '외 N블록 생략' 한 줄로 접는다 — 커밋 폭주 등 비정상 입력에도 요청이 400 나지 않게).
        """

        def _bullet(text: str) -> dict:
            return {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"text": {"content": text[:_MAX_RICH_TEXT]}}]},
            }

        blocks: list[dict] = [
            {
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"text": {"content": _NUTTI_SECTION}}]},
            }
        ]
        for line in report.done.splitlines():
            line = line.strip().removeprefix("- ")
            if line:
                blocks.append(_bullet(line))
        if report.next:
            blocks.append(_bullet(f"내일: {report.next}"))
        blocks.append(_bullet(f"이슈: {report.issues} · 상태: {report.status}"))
        if len(blocks) > _MAX_CHILDREN:
            omitted = len(blocks) - (_MAX_CHILDREN - 1)
            blocks = blocks[: _MAX_CHILDREN - 1] + [_bullet(f"…외 {omitted}블록 생략")]
        return blocks

    def post_nutti_daily(self, report: DailyReport) -> dict:
        """Nutti 일일 업무보고의 그날 페이지에 lead-crawler 섹션을 병기한다.

        페이지 탐색(작성일=날짜)이 필요해 :meth:`_post` 와 달리 2단계다: 있으면 본문에
        블록 append, 없으면 섹션을 본문으로 갖는 페이지를 새로 만든다. dry_run 이면
        네트워크 없이 결정적 payload(제목·블록)를 반환한다.
        """
        db = self.settings.notion_nutti_daily_db
        blocks = self.nutti_daily_blocks(report)
        if not self.enabled:
            log.info("notion.dry_run", what="nutti_daily", db=db)
            return {
                "database_id": db,
                "date": report.date,
                "title": self._nutti_page_title(report.date),
                "children": blocks,
            }
        import httpx

        headers = {
            "Authorization": f"Bearer {self.settings.notion_token}",
            "Notion-Version": self.settings.notion_version,
            "Content-Type": "application/json",
        }
        query = httpx.post(
            _QUERY_API.format(db=db),
            json={
                "filter": {"property": "작성일", "date": {"equals": report.date}},
                "page_size": 1,
            },
            headers=headers,
            timeout=30.0,
        )
        if query.status_code >= 400:
            raise RuntimeError(
                f"notion nutti_daily 조회 실패: HTTP {query.status_code}: {query.text[:300]}"
            )
        results = query.json().get("results", [])
        if results:
            page_id = results[0]["id"]
            if self._nutti_section_exists(page_id, headers):
                # 재실행/재시도 멱등성 — 같은 날 섹션이 이미 있으면 다시 붙이지 않는다.
                log.info("notion.nutti_skipped", date=report.date, page_id=page_id)
                return {"skipped": "already_posted", "page_id": page_id}
            resp = httpx.patch(
                _BLOCKS_API.format(page_id=page_id),
                json={"children": blocks},
                headers=headers,
                timeout=30.0,
            )
        else:
            # ponytail: 동시 이중 실행이면 같은 날짜 페이지 2개 가능(생성 레이스) — 스케줄드
            # 태스크 1일 1회 전제라 락 생략, 문제가 되면 생성 직전 재조회로 보강.
            resp = httpx.post(
                _API,
                json={
                    "parent": {"database_id": db},
                    "properties": {
                        "제목": _title(self._nutti_page_title(report.date)),
                        "작성일": _date(report.date),
                        "Tag": {"multi_select": [{"name": "업무보고"}]},
                    },
                    "children": blocks,
                },
                headers=headers,
                timeout=30.0,
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"notion nutti_daily 전송 실패: HTTP {resp.status_code}: {resp.text[:300]}"
            )
        log.info("notion.posted", what="nutti_daily")
        return resp.json()

    def _nutti_section_exists(self, page_id: str, headers: dict) -> bool:
        """페이지 본문에 lead-crawler 섹션 헤딩이 이미 있는지(커서 페이지네이션 포함)."""
        import httpx

        url = _BLOCKS_API.format(page_id=page_id)
        cursor: str | None = None
        while True:
            params: dict = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = httpx.get(url, params=params, headers=headers, timeout=30.0)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"notion nutti_daily 본문 조회 실패: HTTP {resp.status_code}: {resp.text[:300]}"
                )
            data = resp.json()
            for block in data.get("results", []):
                if block.get("type") != "heading_3":
                    continue
                rich = block.get("heading_3", {}).get("rich_text", [])
                text = "".join(
                    r.get("plain_text") or r.get("text", {}).get("content", "") for r in rich
                )
                if text == _NUTTI_SECTION:
                    return True
            if not data.get("has_more"):
                return False
            cursor = data.get("next_cursor")
