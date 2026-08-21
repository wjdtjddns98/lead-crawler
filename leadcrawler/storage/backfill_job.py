"""백필 작업(backfill_job) 영속화 — 웹 트리거 supervisor·자식 CLI 공용(#352).

crawl_job 과 같은 관례를 따른다: 모든 함수는 호출자 세션 안에서 동작하고 flush 만
한다(commit 은 호출자 책임). 차이는 실행 주체가 프로세스(세대 단위 재기동)라는 것 —
활성 중복은 ``active_track`` 유니크 제약이 DB 레벨에서 막고, 진행 카운터는
``record_progress`` 가 SQL 원자 증가 + generation 펜싱으로 갱신한다(이전 세대 자식의
늦은 보고가 새 세대 카운터를 오염시키지 않게).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..schema import BackfillJobRow

TRACK_A = "A"  # fill-emails(이메일 채우기)
TRACK_C = "C"  # backfill-resolve-domains(도메인 해석→승격)
TRACKS = frozenset({TRACK_A, TRACK_C})

RUNNING = "running"
FAILED = "failed"
CANCELLED = "cancelled"
BUDGET_EXHAUSTED = "budget_exhausted"
# 종료 상태(더 이상 진행/취소 대상 아님). 지속형 consumer 라 'done' 은 없다.
TERMINAL = frozenset({FAILED, CANCELLED, BUDGET_EXHAUSTED})

# 갱신 가능 필드 화이트리스트(임의 컬럼 주입 차단 — crawl_job._UPDATABLE 선례).
# status/finished_at 은 없다 — 종료 전이는 finish_backfill_job 전용(active_track 해제
# 불변식을 우회하면 트랙이 영구 BackfillBusy 에 빠진다, 2026-08-18 Codex MED-1).
_UPDATABLE = frozenset(
    {
        "error",
        "stop_reason",
        "pid",
        "generation",
        "recycles",
        "crash_restarts",
        "remaining",
        "initial_target",
    }
)


class BackfillBusy(Exception):
    """같은 트랙의 활성 작업이 이미 있을 때(동시 1건 가드, API→409)."""

    def __init__(self, track: str, active_id: str | None = None) -> None:
        super().__init__(f"track {track} 활성 작업 존재")
        self.track = track
        self.active_id = active_id


def _new_id() -> str:
    return "bf_" + uuid4().hex[:12]


def backfill_job_dict(row: BackfillJobRow) -> dict[str, object]:
    """작업 행을 DTO 평탄 dict 로(시각은 ISO8601). API 응답·supervisor 스냅샷 공용."""
    return {
        "id": row.id,
        "track": row.track,
        "status": row.status,
        "countries": row.countries,
        "industries": row.industries,
        "exclude_industries": row.exclude_industries,
        "exclude_listed": row.exclude_listed,
        "batch": row.batch,
        "workers": row.workers,
        "max_batches": row.max_batches,
        "min_queue": row.min_queue,
        "initial_target": row.initial_target,
        "remaining": row.remaining,
        "processed": row.processed,
        "resolved": row.resolved,
        "promoted": row.promoted,
        "emails": row.emails,
        "batches_done": row.batches_done,
        "generation": row.generation,
        "recycles": row.recycles,
        "crash_restarts": row.crash_restarts,
        "pid": row.pid,
        "cancel_requested": row.cancel_requested,
        "stop_reason": row.stop_reason,
        "error": row.error,
        "triggered_by": row.triggered_by,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "progress_at": row.progress_at.isoformat() if row.progress_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def create_backfill_job(
    session: Session,
    *,
    track: str,
    countries: str = "",
    industries: str = "",
    exclude_industries: str = "",
    exclude_listed: bool = False,
    batch: int = 200,
    workers: int = 2,
    max_batches: int = 20,
    min_queue: int = 1,
    initial_target: int = 0,
    triggered_by: str | None = None,
) -> BackfillJobRow:
    """새 백필 작업을 만든다(status='running', active_track=track). flush 후 행 반환.

    같은 트랙의 활성 작업이 있으면 ``active_track`` 유니크 위반 → :class:`BackfillBusy`.
    유니크 제약이 정본 가드라 확인-후-삽입 경쟁(TOCTOU)이 없다.
    """
    if track not in TRACKS:
        raise ValueError(f"허용되지 않은 track: {track}")
    if max_batches < 1:
        # 프로세스 리셋(메모리 반환)이 이 기능의 존재 이유 — 무제한(0)은 계약 위반.
        raise ValueError("max_batches 는 1 이상이어야 한다(프로세스 리셋 필수)")
    # 친절 선확인(활성 id 를 409 응답에 실어주기 위함) — 정본 가드는 아래 유니크 제약.
    active = active_backfill_job(session, track)
    if active is not None:
        raise BackfillBusy(track, active.id)
    row = BackfillJobRow(
        id=_new_id(),
        track=track,
        active_track=track,
        status=RUNNING,
        countries=countries.strip(),
        industries=industries.strip(),
        exclude_industries=exclude_industries.strip(),
        exclude_listed=exclude_listed,
        batch=batch,
        workers=workers,
        max_batches=max_batches,
        min_queue=min_queue,
        initial_target=initial_target,
        remaining=initial_target,
        triggered_by=triggered_by,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        # 선확인과 flush 사이에 경쟁자가 먼저 삽입한 경우(TOCTOU) — 유니크 제약이 정본.
        # 세션은 실패 상태로 두고 예외만 올린다(트랜잭션 정리는 호출자 책임 계약).
        raise BackfillBusy(track) from exc
    return row


def get_backfill_job(session: Session, job_id: str) -> BackfillJobRow | None:
    """단건 작업 조회(없으면 None)."""
    return session.get(BackfillJobRow, job_id)


def active_backfill_job(session: Session, track: str) -> BackfillJobRow | None:
    """트랙의 활성 작업 1건(active_track 유니크라 최대 1건)."""
    return session.scalars(
        select(BackfillJobRow).where(BackfillJobRow.active_track == track).limit(1)
    ).first()


def latest_backfill_job(session: Session, track: str) -> BackfillJobRow | None:
    """트랙의 가장 최근 작업 1건(현황 화면 기본 표시용). id 보조정렬로 결정적."""
    return session.scalars(
        select(BackfillJobRow)
        .where(BackfillJobRow.track == track)
        .order_by(BackfillJobRow.started_at.desc(), BackfillJobRow.id.desc())
        .limit(1)
    ).first()


def recent_backfill_jobs(
    session: Session, track: str | None = None, limit: int = 20
) -> list[BackfillJobRow]:
    """최근 작업 목록(최신순, 이력 화면용). track 지정 시 그 트랙만."""
    stmt = select(BackfillJobRow).order_by(
        BackfillJobRow.started_at.desc(), BackfillJobRow.id.desc()
    )
    if track is not None:
        stmt = stmt.where(BackfillJobRow.track == track)
    return list(session.scalars(stmt.limit(limit)))


def record_progress(
    session: Session,
    job_id: str,
    generation: int,
    *,
    processed: int = 0,
    resolved: int = 0,
    promoted: int = 0,
    emails: int = 0,
    batches: int = 0,
    remaining: int | None = None,
) -> bool:
    """자식(--job-id)의 배치 완료 자기보고 — SQL 원자 증가 + generation 펜싱.

    ``column = column + delta`` 로 read-modify-write 경쟁 없이 누적하고, WHERE 에
    ``generation == :g`` 를 넣어 **이전 세대 자식의 늦은 보고를 무시**한다(재기동 직후
    구세대 프로세스가 마지막 배치를 보고하는 경쟁 창). 종료(TERMINAL) 후의 같은 세대
    늦은 보고도 status 조건으로 차단한다(취소 직후 자식의 마지막 배치가 닫힌 통계를
    오염시키는 경쟁 — 2026-08-18 리뷰 MED). 반영됐으면 True.
    """
    now = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "processed": BackfillJobRow.processed + processed,
        "resolved": BackfillJobRow.resolved + resolved,
        "promoted": BackfillJobRow.promoted + promoted,
        "emails": BackfillJobRow.emails + emails,
        "batches_done": BackfillJobRow.batches_done + batches,
        "progress_at": now,
        "updated_at": now,
    }
    if remaining is not None:
        values["remaining"] = remaining
    result = session.execute(
        update(BackfillJobRow)
        .where(
            BackfillJobRow.id == job_id,
            BackfillJobRow.generation == generation,
            BackfillJobRow.status.notin_(TERMINAL),
        )
        .values(**values)
        # 기본 synchronize_session='auto'(evaluate)는 WHERE 를 **로컬 스테일 속성**으로
        # 평가해, DB 가 거부한(rowcount=0) 보고도 세션 내 객체를 오염시킨다(2026-08-18
        # Codex HIGH-1 실증). 로컬 동기화를 끄고 최신값은 호출자가 재조회(refresh)한다.
        .execution_options(synchronize_session=False)
    )
    session.flush()
    return bool(result.rowcount)


def update_backfill_job(
    session: Session, job_id: str, **fields: object
) -> BackfillJobRow | None:
    """작업 상태/세대 필드를 갱신한다(updated_at 자동). 허용 필드만 반영, 없으면 None."""
    row = session.get(BackfillJobRow, job_id)
    if row is None:
        return None
    for key, value in fields.items():
        if key not in _UPDATABLE:
            raise ValueError(f"갱신 불가 필드: {key}")
        setattr(row, key, value)
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    return row


def finish_backfill_job(
    session: Session,
    job_id: str,
    status: str,
    *,
    stop_reason: str | None = None,
    error: str | None = None,
) -> BackfillJobRow | None:
    """작업을 종료 상태로 닫는다 — active_track 해제(다음 작업 시작 허용) + finished_at.

    이미 종료된 작업이면 그대로 반환(멱등 — supervisor 와 reconcile 경로가 겹쳐도 안전).
    """
    if status not in TERMINAL:
        raise ValueError(f"종료 상태가 아님: {status}")
    now = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "status": status,
        "active_track": None,
        "stop_reason": stop_reason,
        "finished_at": now,
        "updated_at": now,
    }
    if error is not None:
        values["error"] = error
    # 원자 first-writer-wins — supervisor 정상종료와 reconcile 이 동시에 닫아도 먼저
    # 커밋한 종료 상태가 보존된다(check-then-write 는 나중 쪽이 덮었다, Codex MED-2).
    session.execute(
        update(BackfillJobRow)
        .where(BackfillJobRow.id == job_id, BackfillJobRow.status.notin_(TERMINAL))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    session.flush()
    row = session.get(BackfillJobRow, job_id)
    if row is not None:
        session.refresh(row)  # 로컬 동기화를 껐으므로 DB 진실로 재적재.
    return row


def request_cancel(session: Session, job_id: str) -> BackfillJobRow | None:
    """활성 작업에 취소를 요청한다(플래그만 — supervisor·자식이 폴링으로 중단).

    이미 종료된 작업이면 그대로 반환(멱등). 없으면 None.
    """
    row = session.get(BackfillJobRow, job_id)
    if row is None:
        return None
    if row.status not in TERMINAL:
        row.cancel_requested = True
        row.updated_at = datetime.now(timezone.utc)
        session.flush()
    return row


def is_cancel_requested(session: Session, job_id: str) -> bool:
    """취소 요청 여부(자식의 협조적 취소 폴링용)."""
    row = session.get(BackfillJobRow, job_id)
    return bool(row and row.cancel_requested)
