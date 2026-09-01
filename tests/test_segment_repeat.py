"""세그먼트 작업 반복 옵션(repeat_every_min·not_before·큐 티커) — 웹 크롤실행 continuous 대체."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

import leadcrawler.pipeline.backfill_process as bp
from leadcrawler.config import Settings
from leadcrawler.storage.backfill_job import (
    DONE,
    QUEUED,
    RUNNING,
    enqueue_repeat_of,
    enqueue_segment_job,
    get_backfill_job,
    next_queued_segment_job,
)
from leadcrawler.storage.db import init_db, session_scope


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    s = Settings(dry_run=False, database_url=f"sqlite:///{tmp_path / 't.db'}",
                 resolve_domains=False)
    init_db(s)
    monkeypatch.setattr(bp, "_running", {"A": False, "C": False, "S": False})
    monkeypatch.setattr(bp, "_budget_exhausted", lambda *_a, **_k: False)
    return s


def _enq(s, **kw):
    kw.setdefault("countries", "JP")
    kw.setdefault("industries", "은행")
    return enqueue_segment_job(s, **kw)


def test_next_queued_skips_future_not_before(settings) -> None:
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    with session_scope(settings) as s:
        later = _enq(s, priority=1, not_before=future)
        ready = _enq(s, priority=50, not_before=past)
        plain = _enq(s, priority=100)
        picked = next_queued_segment_job(s)
        assert picked.id == ready.id  # 우선순위 1인 later 는 not_before 미래라 건너뜀.
        assert plain.id != picked.id and later.status == QUEUED


def test_enqueue_repeat_of_clones_done_repeat_job_once(settings) -> None:
    with session_scope(settings) as s:
        src = _enq(s, repeat_every_min=30, regions="", priority=7)
        src.status = DONE
        s.flush()
        nxt = enqueue_repeat_of(s, src.id)
        assert nxt is not None and nxt.id != src.id
        assert nxt.status == QUEUED and nxt.repeat_every_min == 30 and nxt.priority == 7
        assert nxt.countries == "JP" and nxt.industries == "은행"
        assert nxt.not_before is not None
        assert nxt.not_before.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=29)
        assert enqueue_repeat_of(s, src.id) is None  # 같은 필터 회차가 이미 대기 → 이중 복제 없음.
        # 반복 아님·done 아님 → None.
        once = _enq(s, industries="증권·자산운용")
        once.status = DONE
        s.flush()
        assert enqueue_repeat_of(s, once.id) is None
        running = _enq(s, industries="보험", repeat_every_min=5)
        running.status = RUNNING
        s.flush()
        assert enqueue_repeat_of(s, running.id) is None


class _DoneProc:
    """자식이 즉시 stage=done 을 보고하고 rc=0 으로 끝나는 모형."""

    def __init__(self, settings: Settings, job_id: str) -> None:
        self.pid = 4242
        with session_scope(settings) as s:
            row = get_backfill_job(s, job_id)
            row.stage = "done"

    def poll(self) -> int:
        return 0

    def wait(self) -> int:
        return 0

    def release(self) -> None:
        pass

    def kill_tree(self) -> None:
        pass


def test_done_repeat_job_enqueues_next_round_with_not_before(settings) -> None:
    with session_scope(settings) as s:
        job = _enq(s, repeat_every_min=15)
        job_id = job.id

    def launcher(argv, log_path):  # noqa: ANN001
        jid = argv[argv.index("--job-id") + 1]
        return _DoneProc(settings, jid)

    assert bp.dispatch_next_segment_job(settings, launcher=launcher) == job_id
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with session_scope(settings) as s:
            row = get_backfill_job(s, job_id)
            if row.status == DONE:
                break
        time.sleep(0.05)
    deadline = time.monotonic() + 10  # DONE+복제는 한 트랜잭션 — 두 번째 행이 보일 때까지 폴링.
    while time.monotonic() < deadline:
        with session_scope(settings) as s:
            if s.query(type(row)).filter_by(track="S").count() >= 2:
                break
        time.sleep(0.05)
    with session_scope(settings) as s:
        rows = list(s.query(type(row)).filter_by(track="S").order_by("started_at"))
        assert [r.status for r in rows] == [DONE, QUEUED]
        nxt = rows[1]
        assert nxt.repeat_every_min == 15 and nxt.not_before is not None
        # not_before 가 미래라 finally 의 dispatch 는 건너뛰었다(queued 유지, 활성 없음).
        assert nxt.active_track is None


class _DoneThenCancelProc(_DoneProc):
    """자식이 stage=done 을 보고한 직후(종료 전에) 취소 요청이 들어온 경계 상황."""

    def __init__(self, settings: Settings, job_id: str) -> None:
        super().__init__(settings, job_id)
        with session_scope(settings) as s:
            get_backfill_job(s, job_id).cancel_requested = True


def test_cancel_arriving_at_child_exit_wins_and_no_repeat_is_enqueued(settings) -> None:
    from leadcrawler.storage.backfill_job import CANCELLED

    with session_scope(settings) as s:
        job_id = _enq(s, repeat_every_min=15).id
    assert bp.dispatch_next_segment_job(
        settings, launcher=lambda argv, lp: _DoneThenCancelProc(settings, argv[argv.index("--job-id") + 1])
    ) == job_id
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with session_scope(settings) as s:
            if get_backfill_job(s, job_id).status == CANCELLED:
                break
        time.sleep(0.05)
    time.sleep(0.3)
    with session_scope(settings) as s:
        rows = list(s.query(type(get_backfill_job(s, job_id))).filter_by(track="S"))
        assert [r.status for r in rows] == [CANCELLED]  # DONE 도, 다음 회차도 없음.


def test_not_before_serializes_with_utc_offset_and_future_has_no_queue_position(settings) -> None:
    from leadcrawler.storage.backfill_job import backfill_job_dict, queue_position

    with session_scope(settings) as s:
        ready = _enq(s, priority=1)
        future = _enq(s, priority=0, not_before=datetime.now(timezone.utc) + timedelta(hours=2))
        s.flush()
        s.expire_all()  # SQLite 왕복 후 naive datetime 으로 재적재.
        d = backfill_job_dict(get_backfill_job(s, future.id))
        assert d["not_before"].endswith("+00:00")
        assert queue_position(s, future.id) is None  # 예약행은 순번 없음.
        assert queue_position(s, ready.id) == 1  # 우선순위 0인 예약행이 순번을 안 차지함.


def test_ticker_tick_dispatches_when_not_before_reached(settings) -> None:
    started: list[str] = []

    class _Live:
        pid = 1

        def poll(self):
            return None

        def wait(self):
            return 0

        def release(self):
            pass

        def kill_tree(self):
            pass

    def launcher(argv, log_path):  # noqa: ANN001
        started.append(argv[argv.index("--job-id") + 1])
        return _Live()

    with session_scope(settings) as s:
        future = _enq(s, repeat_every_min=10, not_before=datetime.now(timezone.utc) + timedelta(hours=1))
        future_id = future.id
    assert bp.segment_ticker_tick(settings, launcher=launcher) is None  # 아직 시각 전.
    with session_scope(settings) as s:
        get_backfill_job(s, future_id).not_before = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert bp.segment_ticker_tick(settings, launcher=launcher) == future_id
    deadline = time.monotonic() + 5  # 스폰은 감독 스레드에서 비동기.
    while time.monotonic() < deadline and not started:
        time.sleep(0.05)
    assert started == [future_id]


def test_start_segment_ticker_noop_on_dry_run(monkeypatch) -> None:
    monkeypatch.setattr(bp, "_ticker_started", False)
    assert bp.start_segment_ticker(Settings(dry_run=True)) is False
    assert bp._ticker_started is False
