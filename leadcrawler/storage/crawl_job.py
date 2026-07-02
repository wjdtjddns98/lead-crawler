"""직접 크롤 작업(crawl_job) 영속화 — 웹앱 트리거 + 진행현황 폴링·취소.

웹에서 관리자가 즉시 실행하는 단발 크롤 1건의 상태/카운터를 보관한다. 백그라운드
스레드가 카운터를 갱신하고, 취소 요청은 ``cancel_requested`` 플래그로 전달된다.
모든 함수는 호출자가 넘긴 세션 안에서 동작하고 flush 만 한다(commit 은 호출자 책임).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..schema import CrawlJobRow

RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
# 종료 상태(더 이상 진행/취소 대상 아님).
TERMINAL = frozenset({DONE, FAILED, CANCELLED})

# 실행 모드 — once(단발 1회전) | continuous(취소까지 라운드 반복).
MODE_ONCE = "once"
MODE_CONTINUOUS = "continuous"

# 갱신 가능한 카운터·상태 필드 화이트리스트(임의 컬럼 주입 차단).
_UPDATABLE = frozenset(
    {
        "status",
        "segments_total",
        "segments_done",
        "discovered",
        "enriched",
        "saved",
        "rounds_done",
        "error",
        "finished_at",
    }
)


def _new_id() -> str:
    return "cj_" + uuid4().hex[:12]


def crawl_job_dict(row: CrawlJobRow) -> dict[str, object]:
    """작업 행을 DTO 평탄 dict 로(시각은 ISO8601). API 응답·스레드 스냅샷 공용.

    세션이 닫히기 전에 호출해 detached 접근을 피한다(스레드가 own 세션에서 만든 행도 안전).
    """
    return {
        "id": row.id,
        "status": row.status,
        "countries": row.countries,
        "industries": row.industries,
        "listed": row.listed,
        "persist": row.persist,
        "segments_total": row.segments_total,
        "segments_done": row.segments_done,
        "discovered": row.discovered,
        "enriched": row.enriched,
        "saved": row.saved,
        "mode": row.mode,
        "rounds_done": row.rounds_done,
        "error": row.error,
        "cancel_requested": row.cancel_requested,
        "triggered_by": row.triggered_by,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def create_crawl_job(
    session: Session,
    *,
    countries: str,
    industries: str,
    listed: str,
    persist: bool,
    segments_total: int,
    triggered_by: str | None,
    mode: str = MODE_ONCE,
) -> CrawlJobRow:
    """새 크롤 작업 행을 만든다(status='running'). flush 후 행 반환."""
    row = CrawlJobRow(
        id=_new_id(),
        status=RUNNING,
        countries=countries.strip(),
        industries=industries.strip(),
        listed=listed,
        persist=persist,
        segments_total=segments_total,
        triggered_by=triggered_by,
        mode=mode,
    )
    session.add(row)
    session.flush()
    return row


def get_crawl_job(session: Session, job_id: str) -> CrawlJobRow | None:
    """단건 작업 조회(없으면 None)."""
    return session.get(CrawlJobRow, job_id)


def latest_crawl_job(session: Session) -> CrawlJobRow | None:
    """가장 최근 시작된 작업 1건(현황 화면 기본 표시용).

    같은 시각(틱)에 만들어진 행이 있어도 id 보조정렬로 결정적으로 1건을 고른다.
    """
    return session.scalars(
        select(CrawlJobRow)
        .order_by(CrawlJobRow.started_at.desc(), CrawlJobRow.id.desc())
        .limit(1)
    ).first()


def active_crawl_job(session: Session) -> CrawlJobRow | None:
    """진행 중(running) 작업 1건(동시 1건 가드·취소 대상). id 보조정렬로 결정적."""
    return session.scalars(
        select(CrawlJobRow)
        .where(CrawlJobRow.status == RUNNING)
        .order_by(CrawlJobRow.started_at.desc(), CrawlJobRow.id.desc())
        .limit(1)
    ).first()


def fail_running_jobs(session: Session, reason: str) -> int:
    """남아있는 running 행을 모두 failed 로 일괄 정리한다(반환=정리 건수).

    crawl_job 은 웹 트리거 전용(스케줄러는 이 테이블을 안 쓴다)이라, 살아있는 실행
    스레드가 없을 때(_running=False) 남은 running 행은 전부 비정상 종료 잔재다 — 하나만
    실패시키면 다중 잔재가 누적돼 현황·가드를 오염시키므로 전부 정리한다.
    """
    now = datetime.now(timezone.utc)
    result = session.execute(
        update(CrawlJobRow)
        .where(CrawlJobRow.status == RUNNING)
        .values(status=FAILED, error=reason, finished_at=now, updated_at=now)
    )
    session.flush()
    return int(result.rowcount or 0)


def update_crawl_job(session: Session, job_id: str, **fields: object) -> CrawlJobRow | None:
    """작업 카운터/상태를 갱신한다(updated_at 자동). 허용 필드만 반영, 없으면 None."""
    row = session.get(CrawlJobRow, job_id)
    if row is None:
        return None
    for key, value in fields.items():
        if key not in _UPDATABLE:
            raise ValueError(f"갱신 불가 필드: {key}")
        setattr(row, key, value)
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    return row


def request_cancel(session: Session, job_id: str) -> CrawlJobRow | None:
    """진행 중 작업에 취소를 요청한다(플래그만 설정 — 실행 스레드가 다음 폴링에서 중단).

    이미 종료된 작업이면 그대로 반환(멱등). 없으면 None.
    """
    row = session.get(CrawlJobRow, job_id)
    if row is None:
        return None
    if row.status == RUNNING:
        row.cancel_requested = True
        row.updated_at = datetime.now(timezone.utc)
        session.flush()
    return row


def is_cancel_requested(session: Session, job_id: str) -> bool:
    """취소 요청 여부를 읽는다(실행 스레드의 협조적 취소 폴링용)."""
    row = session.get(CrawlJobRow, job_id)
    return bool(row and row.cancel_requested)
