"""트랙 S(세그먼트 승격 큐) 스토리지 계층(segment-jobs-design §2 PR2).

enqueue→activate 전이·active_track UNIQUE 경쟁, 대기열 정렬, pause/requeue, 진행 자기보고
(stage/cursor/discovered/failed_items) 세대 펜싱, 우선순위 가드, 트랙 A 기존 흐름 회귀 없음.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from leadcrawler.config import Settings
from leadcrawler.storage.backfill_job import (
    CANCELLED,
    DONE,
    FAILED,
    PAUSED,
    QUEUED,
    RUNNING,
    BackfillBusy,
    activate_segment_job,
    backfill_job_dict,
    create_backfill_job,
    enqueue_segment_job,
    finish_backfill_job,
    next_queued_segment_job,
    pause_backfill_job,
    queue_position,
    record_progress,
    request_cancel,
    requeue_segment_job,
    set_segment_priority,
    update_backfill_job,
)
from leadcrawler.schema import BackfillJobRow
from leadcrawler.storage.db import init_db, session_scope


@pytest.fixture
def session(tmp_path) -> Session:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/segjobs.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        yield s


def test_enqueue_then_activate_and_active_track_unique(session: Session) -> None:
    """enqueue 는 대기(active_track=NULL) — activate 가 원자 전이하고 두 번째는 BackfillBusy."""
    a = enqueue_segment_job(session, countries="KR", industries="증권·자산운용")
    b = enqueue_segment_job(session, countries="US", industries="바이오")
    assert a.status == QUEUED and a.active_track is None
    assert b.status == QUEUED and b.active_track is None

    activated = activate_segment_job(session, a.id)
    assert activated is not None
    assert activated.status == RUNNING and activated.active_track == "S"
    session.commit()  # 디스패처 계약: activate 는 짧은 단독 트랜잭션으로 커밋.

    with pytest.raises(BackfillBusy):
        activate_segment_job(session, b.id)
    # 유니크 위반 후 세션은 rollback 된 상태 — 그대로 계속 쓸 수 있어야 한다(PG abort 계약).
    b = session.get(BackfillJobRow, b.id)
    session.refresh(b)
    assert b.status == QUEUED and b.active_track is None  # 실패한 activate 는 무변경.

    # 이미 실행 중/종료된 작업의 재활성은 대상 없음(0행) → None.
    assert activate_segment_job(session, a.id) is None
    assert activate_segment_job(session, "bf_missing") is None


def test_next_queued_orders_by_priority_then_started_then_id(session: Session) -> None:
    """정렬 = priority 낮은 순 → started_at → id."""
    low = enqueue_segment_job(session, countries="KR", industries="바이오", priority=50)
    high = enqueue_segment_job(session, countries="KR", industries="반도체", priority=200)
    tie1 = enqueue_segment_job(session, countries="KR", industries="화학", priority=100)
    tie2 = enqueue_segment_job(session, countries="KR", industries="철강", priority=100)
    # 같은 priority 는 started_at(요청 시각) 빠른 순 — 명시적으로 tie2 를 더 먼저 요청한 것으로.
    t0 = datetime.now(timezone.utc)
    tie1.started_at, tie2.started_at = t0, t0 - timedelta(minutes=1)
    session.flush()
    assert next_queued_segment_job(session).id == low.id
    activate_segment_job(session, low.id)  # 큐에서 빠지면 다음 후보가 이어진다.
    assert next_queued_segment_job(session).id == tie2.id
    assert queue_position(session, tie1.id) == 2
    assert queue_position(session, high.id) == 3  # 우선순위 200 은 맨 뒤.


def test_pause_preserves_cursor_and_counters_then_requeue_and_reactivate(
    session: Session,
) -> None:
    """pause 는 커서·카운터 보존 + active_track 해제 → requeue → 재활성 가능."""
    job = enqueue_segment_job(session, countries="KR", industries="바이오")
    activate_segment_job(session, job.id)
    assert record_progress(
        session, job.id, 0, processed=5, discovered=9, cursor="dom:kr:x.co.kr", stage="promote"
    )
    session.refresh(job)
    assert (job.processed, job.discovered, job.promote_cursor, job.stage) == (
        5,
        9,
        "dom:kr:x.co.kr",
        "promote",
    )

    gen_before = job.generation
    paused = pause_backfill_job(session, job.id)
    assert paused.status == PAUSED and paused.active_track is None
    assert paused.stop_reason == "pause" and paused.cancel_requested is False
    # 세대 bump — 살아남은 구 자식의 보고는 거부(paused 도 running 이 아니라 어차피 거부).
    assert paused.generation == gen_before + 1
    assert not record_progress(session, job.id, gen_before, processed=1)
    assert not record_progress(session, job.id, paused.generation, processed=1)
    # 커서·카운터는 그대로.
    assert (paused.processed, paused.discovered, paused.promote_cursor, paused.stage) == (
        5,
        9,
        "dom:kr:x.co.kr",
        "promote",
    )

    # queued 상태에서 pause 도 지원(아직 activate 전).
    other = enqueue_segment_job(session, countries="US", industries="반도체")
    paused_queued = pause_backfill_job(session, other.id)
    assert paused_queued.status == PAUSED

    requeued = requeue_segment_job(session, job.id)
    assert requeued.status == QUEUED and requeued.error is None
    assert requeued.promote_cursor == "dom:kr:x.co.kr"  # 커서 보존.

    reactivated = activate_segment_job(session, job.id)
    assert reactivated.status == RUNNING and reactivated.active_track == "S"

    # 대상 상태가 아니면 pause/requeue 모두 None.
    assert pause_backfill_job(session, job.id + "_nope") is None
    assert requeue_segment_job(session, reactivated.id) is None  # running 은 requeue 대상 아님.

    # failed·budget_exhausted 도 requeue 대상 — 이전 실행 잔재(에러·취소 플래그·종료시각·
    # 정지사유·pid) 초기화, 커서 보존.
    request_cancel(session, reactivated.id)
    update_backfill_job(session, reactivated.id, pid=4242)
    finish_backfill_job(session, reactivated.id, FAILED, error="네트워크 오류", stop_reason="crash")
    from_failed = requeue_segment_job(session, reactivated.id)
    assert from_failed.status == QUEUED and from_failed.error is None
    assert from_failed.cancel_requested is False and from_failed.finished_at is None
    assert from_failed.stop_reason is None and from_failed.pid is None
    assert from_failed.promote_cursor == "dom:kr:x.co.kr"


def test_record_progress_generation_fencing_and_terminal_rejects(session: Session) -> None:
    """stage/cursor/discovered/failed_items 도 같은 세대·비종료 상태만 반영된다."""
    job = enqueue_segment_job(session, countries="KR", industries="바이오")
    activate_segment_job(session, job.id)

    assert record_progress(session, job.id, 0, discovered=3, failed_items=1, stage="discover")
    session.refresh(job)
    assert (job.discovered, job.failed_items, job.stage) == (3, 1, "discover")

    # 세대 교체 후 구세대(0) 보고는 거부.
    update_backfill_job(session, job.id, generation=1)
    assert not record_progress(session, job.id, 0, discovered=99, stage="promote")
    session.refresh(job)
    assert job.discovered == 3 and job.stage == "discover"

    # 신세대(1) 보고는 반영.
    assert record_progress(session, job.id, 1, discovered=2, stage="promote", cursor="k1")
    session.refresh(job)
    assert (job.discovered, job.stage, job.promote_cursor) == (5, "promote", "k1")

    # 종료 후 같은 세대의 늦은 보고는 거부(닫힌 통계 불변).
    finish_backfill_job(session, job.id, DONE)
    assert not record_progress(session, job.id, 1, discovered=1, failed_items=1)
    session.refresh(job)
    assert job.discovered == 5 and job.failed_items == 1


def test_set_segment_priority_guards_running(session: Session) -> None:
    """priority 갱신은 queued|paused 만 — running 이면 None 반환하고 값도 안 바뀐다."""
    job = enqueue_segment_job(session, countries="KR", industries="바이오", priority=100)
    updated = set_segment_priority(session, job.id, 10)
    assert updated.priority == 10

    activate_segment_job(session, job.id)
    assert set_segment_priority(session, job.id, 999) is None
    session.refresh(job)
    assert job.priority == 10  # running 중엔 불변.

    pause_backfill_job(session, job.id)
    paused_update = set_segment_priority(session, job.id, 5)
    assert paused_update.priority == 5


def test_queue_position(session: Session) -> None:
    """대기열 순번(1부터) — queued 아니면 None."""
    a = enqueue_segment_job(session, countries="KR", industries="바이오", priority=100)
    b = enqueue_segment_job(session, countries="KR", industries="반도체", priority=100)
    c = enqueue_segment_job(session, countries="KR", industries="화학", priority=50)
    # c 가 priority 50 으로 맨 앞.
    assert queue_position(session, c.id) == 1
    assert queue_position(session, a.id) == 2
    assert queue_position(session, b.id) == 3
    activate_segment_job(session, c.id)
    assert queue_position(session, c.id) is None  # running 은 대기열 소속 아님.
    assert queue_position(session, a.id) == 1
    assert queue_position(session, "bf_missing") is None


def test_track_a_flow_unaffected(session: Session) -> None:
    """트랙 A 의 기존 create→record_progress→finish 흐름은 회귀 없음(TERMINAL 확장 무관)."""
    job = create_backfill_job(session, track="A", countries="KR", triggered_by="admin")
    assert job.status == RUNNING and job.active_track == "A"
    assert record_progress(session, job.id, 0, processed=10, emails=2, batches=1, remaining=5)
    session.refresh(job)
    assert (job.processed, job.emails, job.remaining) == (10, 2, 5)
    # 신규 컬럼 기본값도 dict 스냅샷에 반영되고, A 는 stage/listed 등 건드리지 않는다.
    d = backfill_job_dict(job)
    assert d["listed"] == "unknown" and d["stage"] == "" and d["discovered"] == 0
    finished = finish_backfill_job(session, job.id, CANCELLED, stop_reason="operator")
    assert finished.status == CANCELLED and finished.active_track is None
    # A 는 여전히 create_backfill_job 재사용 가능(활성 슬롯 반환됨).
    again = create_backfill_job(session, track="A")
    assert again.status == RUNNING

    with pytest.raises(ValueError):
        create_backfill_job(session, track="B")
    with pytest.raises(ValueError):
        create_backfill_job(session, track="A", max_batches=0)
    # S 는 즉시실행 경로 금지(대기열·우선순위·stage 초기값 우회 방지) — enqueue 전용.
    with pytest.raises(ValueError):
        create_backfill_job(session, track="S")
    with pytest.raises(ValueError):
        enqueue_segment_job(session, countries="KR", industries="바이오", max_batches=0)


def test_segment_helpers_do_not_touch_track_a_jobs(session: Session) -> None:
    """S 헬퍼(pause/requeue/priority/activate/queue_position)는 A/C id 에 무변경(None)."""
    a = create_backfill_job(session, track="A", countries="KR")
    assert pause_backfill_job(session, a.id) is None
    assert set_segment_priority(session, a.id, 1) is None
    assert activate_segment_job(session, a.id) is None
    assert queue_position(session, a.id) is None
    finish_backfill_job(session, a.id, FAILED, error="x")
    assert requeue_segment_job(session, a.id) is None
    session.refresh(a)
    assert a.status == FAILED and a.track == "A" and a.error == "x"
