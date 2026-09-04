"""백필 작업(backfill_job) 영속화 — 웹 트리거 supervisor·자식 CLI 공용(#352).

crawl_job 과 같은 관례를 따른다: 모든 함수는 호출자 세션 안에서 동작하고 flush 만
한다(commit 은 호출자 책임). 차이는 실행 주체가 프로세스(세대 단위 재기동)라는 것 —
활성 중복은 ``active_track`` 유니크 제약이 DB 레벨에서 막고, 진행 카운터는
``record_progress`` 가 SQL 원자 증가 + generation 펜싱으로 갱신한다(이전 세대 자식의
늦은 보고가 새 세대 카운터를 오염시키지 않게).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..schema import BackfillJobRow

TRACK_A = "A"  # fill-emails(이메일 채우기)
TRACK_C = "C"  # backfill-resolve-domains(도메인 해석→승격)
TRACK_S = "S"  # segment-run(세그먼트 지정 발견→승격 큐)
TRACKS = frozenset({TRACK_A, TRACK_C, TRACK_S})
# 즉시 실행(create→running) 가능한 트랙. S 는 enqueue→activate 2단계만 허용(대기열·우선순위·
# stage 초기값을 건너뛰는 우회 방지 — 리뷰 MED).
_IMMEDIATE_TRACKS = frozenset({TRACK_A, TRACK_C})

RUNNING = "running"
FAILED = "failed"
CANCELLED = "cancelled"
BUDGET_EXHAUSTED = "budget_exhausted"
# 트랙 S 전용 상태 — A/C 는 대기열 없이 즉시 running 이라 쓰지 않는다.
QUEUED = "queued"
PAUSED = "paused"
DONE = "done"
# 종료 상태(더 이상 진행/취소 대상 아님). A/C 는 지속형(consumer)이라 done 을 보고하지
# 않는다 — DONE 을 TERMINAL 에 넣어도 기존 트랙 흐름은 무영향(회귀 테스트로 고정).
TERMINAL = frozenset({FAILED, CANCELLED, BUDGET_EXHAUSTED, DONE})

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


def _iso_utc(dt: datetime | None) -> str | None:
    """tz 없는 값(SQLite 는 naive 로 돌려줌)은 UTC 로 간주해 항상 offset 포함 ISO 로."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def is_ready(row: BackfillJobRow, now: datetime | None = None) -> bool:
    """대기 행이 지금 실행 가능한가(not_before 없음 또는 도래). naive 값은 UTC 로 본다."""
    nb = getattr(row, "not_before", None)
    if nb is None:
        return True
    if nb.tzinfo is None:
        nb = nb.replace(tzinfo=timezone.utc)
    return nb <= (now or datetime.now(timezone.utc))


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
        "listed": row.listed,
        "regions": row.regions,
        "batch": row.batch,
        "workers": row.workers,
        "max_batches": row.max_batches,
        "min_queue": row.min_queue,
        "priority": row.priority,
        "initial_target": row.initial_target,
        "remaining": row.remaining,
        "processed": row.processed,
        "resolved": row.resolved,
        "promoted": row.promoted,
        "emails": row.emails,
        "batches_done": row.batches_done,
        "stage": row.stage,
        "discovered": row.discovered,
        "promote_cursor": row.promote_cursor,
        "failed_items": row.failed_items,
        "generation": row.generation,
        "recycles": row.recycles,
        "crash_restarts": row.crash_restarts,
        "pid": row.pid,
        "cancel_requested": row.cancel_requested,
        "stop_reason": row.stop_reason,
        "error": row.error,
        "triggered_by": row.triggered_by,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "repeat_every_min": int(getattr(row, "repeat_every_min", 0) or 0),
        "not_before": _iso_utc(getattr(row, "not_before", None)),
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
    if track not in _IMMEDIATE_TRACKS:
        raise ValueError(f"허용되지 않은 track: {track} (S 는 enqueue_segment_job 전용)")
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
    stage: str | None = None,
    cursor: str | None = None,
    discovered: int = 0,
    failed_items: int = 0,
    initial_target: int | None = None,
) -> bool:
    """자식(--job-id)의 배치 완료 자기보고 — SQL 원자 증가 + generation 펜싱.

    ``column = column + delta`` 로 read-modify-write 경쟁 없이 누적하고, WHERE 에
    ``generation == :g`` 를 넣어 **이전 세대 자식의 늦은 보고를 무시**한다(재기동 직후
    구세대 프로세스가 마지막 배치를 보고하는 경쟁 창). 종료(TERMINAL) 후의 같은 세대
    늦은 보고도 status 조건으로 차단한다(취소 직후 자식의 마지막 배치가 닫힌 통계를
    오염시키는 경쟁 — 2026-08-18 리뷰 MED). 반영됐으면 True.

    ``stage``/``cursor``(트랙 S 전용)는 최신값 SET(제공 시에만) — ``discovered``/
    ``failed_items`` 는 다른 카운터처럼 누적.
    """
    now = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "processed": BackfillJobRow.processed + processed,
        "resolved": BackfillJobRow.resolved + resolved,
        "promoted": BackfillJobRow.promoted + promoted,
        "emails": BackfillJobRow.emails + emails,
        "batches_done": BackfillJobRow.batches_done + batches,
        "discovered": BackfillJobRow.discovered + discovered,
        "failed_items": BackfillJobRow.failed_items + failed_items,
        "progress_at": now,
        "updated_at": now,
    }
    if remaining is not None:
        values["remaining"] = remaining
    if initial_target is not None:
        # 트랙 S: 적재 시점엔 발견 전이라 대상 수를 모른다 — discover→promote 전이 보고가 확정
        # (진행률 분모, 설계 §5). A/C 는 create 시점에 세팅하므로 넘기지 않는다.
        values["initial_target"] = initial_target
    if stage is not None:
        values["stage"] = stage
    if cursor is not None:
        values["promote_cursor"] = cursor
    result = session.execute(
        update(BackfillJobRow)
        .where(
            BackfillJobRow.id == job_id,
            BackfillJobRow.generation == generation,
            # running 만 반영 — 종료 상태는 물론 paused/queued(트랙 S)도 거부한다(일시정지
            # 된 잡의 커서·카운터를 살아남은 구 자식이 계속 바꾸는 것 방지 — 리뷰 HIGH).
            BackfillJobRow.status == RUNNING,
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


# --- 트랙 S(세그먼트 승격 큐) 전용 헬퍼 -----------------------------------------
# A/C 와 달리 여러 건이 queued 로 동시 대기할 수 있다(active_track 유니크는 running
# 1건만 강제) — 생성(enqueue)과 실행 전이(activate)를 분리한다.


def enqueue_segment_job(
    session: Session,
    *,
    countries: str,
    industries: str,
    listed: str = "unknown",
    regions: str = "",
    priority: int = 100,
    batch: int = 100,
    workers: int = 3,
    max_batches: int = 20,
    triggered_by: str | None = None,
    repeat_every_min: int = 0,
    not_before: datetime | None = None,
) -> BackfillJobRow:
    """세그먼트 승격 요청을 대기열에 적재한다(status=queued, active_track=NULL).

    active_track 이 비어 있어 유니크 제약과 무관 — 여러 건 동시 대기 가능. 실행은
    ``activate_segment_job`` 이 원자 전이로 트랙 S 활성 슬롯을 점유한다.
    ``repeat_every_min>0`` 이면 done 뒤 같은 필터로 다음 잡이 ``not_before`` 를 두고 복제된다.
    """
    if repeat_every_min < 0:
        raise ValueError("repeat_every_min 은 0 이상이어야 한다")
    if max_batches < 1:
        # create_backfill_job 과 동일 — 세대 리셋 없는 무한 자식은 2026-08-03 메모리 폭주 재현.
        raise ValueError("max_batches 는 1 이상이어야 한다(프로세스 리셋 필수)")
    if not countries.strip() or not industries.strip():
        # 빈 필터는 _scoped 가 "무필터"로 흡수해 전 우주 승격이 돈다(리뷰 MED) — 적재부터 거부.
        raise ValueError("countries 와 industries 는 비어 있을 수 없다")
    row = BackfillJobRow(
        id=_new_id(),
        track=TRACK_S,
        active_track=None,
        status=QUEUED,
        countries=countries.strip(),
        industries=industries.strip(),
        listed=listed,
        regions=regions.strip(),
        priority=priority,
        batch=batch,
        workers=workers,
        max_batches=max_batches,
        triggered_by=triggered_by,
        repeat_every_min=repeat_every_min,
        not_before=not_before,
    )
    session.add(row)
    session.flush()
    return row


def enqueue_repeat_of(session: Session, job_id: str) -> BackfillJobRow | None:
    """done 으로 끝난 반복 잡의 다음 회차를 같은 필터로 적재한다(not_before=now+간격).

    반복이 아니거나(0)·done 이 아니거나·이미 다음 회차가 대기/실행 중(같은 필터·반복값의
    queued/running 존재)이면 None — 감독 스레드 재진입·재기동 재개로 이중 복제되지 않게.
    """
    src = session.get(BackfillJobRow, job_id)
    if src is None or src.track != TRACK_S or src.status != DONE or not src.repeat_every_min:
        return None
    dup = session.scalars(
        select(BackfillJobRow).where(
            BackfillJobRow.track == TRACK_S,
            BackfillJobRow.status.in_((QUEUED, RUNNING)),
            BackfillJobRow.countries == src.countries,
            BackfillJobRow.industries == src.industries,
            BackfillJobRow.listed == src.listed,
            BackfillJobRow.regions == src.regions,
            BackfillJobRow.repeat_every_min == src.repeat_every_min,
        ).limit(1)
    ).first()
    if dup is not None:
        return None
    return enqueue_segment_job(
        session, countries=src.countries, industries=src.industries, listed=src.listed,
        regions=src.regions, priority=src.priority, batch=src.batch, workers=src.workers,
        max_batches=src.max_batches, triggered_by=src.triggered_by,
        repeat_every_min=src.repeat_every_min,
        not_before=datetime.now(timezone.utc) + timedelta(minutes=src.repeat_every_min),
    )


def activate_segment_job(session: Session, job_id: str) -> BackfillJobRow | None:
    """대기 중인 세그먼트 작업을 실행으로 전이한다(원자 UPDATE).

    ``WHERE status='queued'`` 로 이미 실행 중이거나 종료된 작업의 재전이를 막고,
    ``active_track`` 유니크 위반(다른 S 작업이 먼저 activate)이면 :class:`BackfillBusy` 로
    승격한다. 대상이 없으면(0행) None.

    **세션 계약**: 유니크 위반 시 PostgreSQL 은 트랜잭션 전체를 abort 하므로 여기서
    ``session.rollback()`` 을 호출한다 — 같은 트랜잭션의 선행 미커밋 변경도 함께 사라진다.
    호출자(디스패처)는 activate 를 **짧은 단독 트랜잭션**으로 돌려야 한다: 승자가 미커밋이면
    PG 유니크 인덱스에서 패자가 승자 커밋까지 대기(블로킹)한다(리뷰 MED 실증).
    """
    now = datetime.now(timezone.utc)
    try:
        result = session.execute(
            update(BackfillJobRow)
            .where(
                BackfillJobRow.id == job_id,
                BackfillJobRow.track == TRACK_S,
                BackfillJobRow.status == QUEUED,
            )
            .values(status=RUNNING, active_track=TRACK_S, updated_at=now)
            .execution_options(synchronize_session=False)
        )
    except IntegrityError as exc:
        session.rollback()  # PG: abort 된 트랜잭션을 정리해야 세션이 계속 쓸 수 있다.
        raise BackfillBusy(TRACK_S) from exc
    session.flush()
    if not result.rowcount:
        return None
    row = session.get(BackfillJobRow, job_id)
    if row is not None:
        session.refresh(row)  # 로컬 동기화를 껐으므로 DB 진실로 재적재.
    return row


def next_queued_segment_job(session: Session) -> BackfillJobRow | None:
    """대기열 맨 앞 작업(우선순위 낮은 순 → 생성순 → id) — 디스패처가 activate 대상 선정.

    ``not_before`` 가 미래인 반복 복제분은 건너뛴다(시각이 되면 큐 티커가 집어 간다).
    """
    now = datetime.now(timezone.utc)
    return session.scalars(
        select(BackfillJobRow)
        .where(
            BackfillJobRow.track == TRACK_S,
            BackfillJobRow.status == QUEUED,
            or_(BackfillJobRow.not_before.is_(None), BackfillJobRow.not_before <= now),
        )
        .order_by(BackfillJobRow.priority, BackfillJobRow.started_at, BackfillJobRow.id)
        .limit(1)
    ).first()


def pause_backfill_job(session: Session, job_id: str) -> BackfillJobRow | None:
    """실행 중/대기 중인 트랙 S 작업을 일시정지한다 — 커서·카운터 보존, active_track 해제.

    running|queued 가 아니면(이미 종료·paused) 대상 없음 → None. **generation 을 올린다**
    — running 잡의 자식이 아직 살아 있어도 ``should_stop``(세대 불일치)으로 스스로 멈추고,
    ``record_progress`` 도 구세대 보고를 거부한다(리뷰 HIGH: 슬롯 해제 후 구 자식이 계속
    과금·커서 갱신하는 사고 차단). 커서는 배치 persist 후 기록이라 유실 없음.
    트랙 S 전용 — A/C id 로 호출하면 무변경(None).
    """
    result = session.execute(
        update(BackfillJobRow)
        .where(
            BackfillJobRow.id == job_id,
            BackfillJobRow.track == TRACK_S,
            BackfillJobRow.status.in_((RUNNING, QUEUED)),
        )
        .values(
            status=PAUSED,
            active_track=None,
            cancel_requested=False,
            stop_reason="pause",
            generation=BackfillJobRow.generation + 1,
            updated_at=datetime.now(timezone.utc),
        )
        .execution_options(synchronize_session=False)
    )
    session.flush()
    if not result.rowcount:
        return None
    row = session.get(BackfillJobRow, job_id)
    if row is not None:
        session.refresh(row)
    return row


def requeue_segment_job(session: Session, job_id: str) -> BackfillJobRow | None:
    """일시정지·실패·예산소진·취소 트랙 S 작업을 대기열로 되돌린다(커서·카운터 보존).

    이전 실행의 잔재(에러·취소 플래그·종료시각·정지사유·pid)를 지운다 — 취소 플래그가 남으면
    재활성 직후 자식이 ``should_stop`` 으로 즉시 종료되는 조용한 루프가 생긴다(리뷰 MED).
    """
    result = session.execute(
        update(BackfillJobRow)
        .where(
            BackfillJobRow.id == job_id,
            BackfillJobRow.track == TRACK_S,
            BackfillJobRow.status.in_((PAUSED, FAILED, BUDGET_EXHAUSTED, CANCELLED)),
        )
        .values(
            status=QUEUED,
            active_track=None,
            error=None,
            cancel_requested=False,
            finished_at=None,
            stop_reason=None,
            pid=None,
            # 세대 bump — 취소된 잡의 (혹시 남은) 구 자식 보고를 펜싱(PO 결정 2026-08-26: 취소건도
            # 커서 보존 재개 허용 — 실수로 취소한 잡을 처음부터 다시 발견하지 않게).
            generation=BackfillJobRow.generation + 1,
            updated_at=datetime.now(timezone.utc),
        )
        .execution_options(synchronize_session=False)
    )
    session.flush()
    if not result.rowcount:
        return None
    row = session.get(BackfillJobRow, job_id)
    if row is not None:
        session.refresh(row)
    return row


def cancel_idle_segment_job(session: Session, job_id: str) -> BackfillJobRow | None:
    """queued|paused 트랙 S 작업을 즉시 CANCELLED 로 닫는다(원자 조건부 UPDATE).

    API 가 "queued 를 읽고 → finish" 로 두 단계로 처리하면 그 사이 디스패처가 activate 한
    running 잡을 강제 종료해 자식이 고아가 된다(리뷰 HIGH TOCTOU). running 이면 0행(None) —
    호출자는 협조 취소(``request_cancel``)로 돌린다.
    """
    result = session.execute(
        update(BackfillJobRow)
        .where(
            BackfillJobRow.id == job_id,
            BackfillJobRow.track == TRACK_S,
            BackfillJobRow.status.in_((QUEUED, PAUSED)),
        )
        .values(
            status=CANCELLED,
            active_track=None,
            stop_reason="operator",
            finished_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        .execution_options(synchronize_session=False)
    )
    session.flush()
    if not result.rowcount:
        return None
    row = session.get(BackfillJobRow, job_id)
    if row is not None:
        session.refresh(row)
    return row


def set_segment_priority(session: Session, job_id: str, priority: int) -> BackfillJobRow | None:
    """대기열 우선순위를 바꾼다 — queued|paused 인 트랙 S 작업만(실행 중 재정렬은 무의미).

    다른 헬퍼와 같은 원자 UPDATE(상태 조건을 WHERE 에) — get→대입은 확인과 flush 사이에
    activate 가 끼어들면 running 행에 써 버린다(리뷰 LOW TOCTOU).
    """
    result = session.execute(
        update(BackfillJobRow)
        .where(
            BackfillJobRow.id == job_id,
            BackfillJobRow.track == TRACK_S,
            BackfillJobRow.status.in_((QUEUED, PAUSED)),
        )
        .values(priority=priority, updated_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    session.flush()
    if not result.rowcount:
        return None
    row = session.get(BackfillJobRow, job_id)
    if row is not None:
        session.refresh(row)
    return row


def queue_position(session: Session, job_id: str) -> int | None:
    """대기열 내 순번(1부터) — queued 가 아니거나 not_before 미래(예약 대기)면 None.

    예약 행이 순번을 차지하면 실제 다음 실행 잡이 2번으로 보인다(Codex 리뷰 LOW).
    """
    now = datetime.now(timezone.utc)
    ordered_ids = session.scalars(
        select(BackfillJobRow.id)
        .where(
            BackfillJobRow.track == TRACK_S,
            BackfillJobRow.status == QUEUED,
            or_(BackfillJobRow.not_before.is_(None), BackfillJobRow.not_before <= now),
        )
        .order_by(BackfillJobRow.priority, BackfillJobRow.started_at, BackfillJobRow.id)
    ).all()
    try:
        return ordered_ids.index(job_id) + 1
    except ValueError:  # 대기열에 없음(queued 아님·다른 트랙·미존재).
        return None
