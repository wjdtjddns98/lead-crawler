"""트랙 S(세그먼트 승격 큐) supervisor 배선 — done/pause/resume/dispatch(#352 PR4).

가짜 런처 주입(``tests/test_backfill_supervisor.py`` 관례) — 실 프로세스 스폰 없음.
"""

from __future__ import annotations

import time

import pytest

import leadcrawler.pipeline.backfill_process as bp
from leadcrawler.config import Settings
from leadcrawler.storage.backfill_job import (
    DONE,
    PAUSED,
    QUEUED,
    RUNNING,
    activate_segment_job,
    enqueue_segment_job,
    get_backfill_job,
    record_progress,
    request_cancel,
)
from leadcrawler.storage.db import get_sessionmaker, init_db


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    s = Settings(database_url=f"sqlite:///{tmp_path}/sv.db", dry_run=True)
    init_db(s)
    bp._reset_running_guard_for_tests()
    monkeypatch.setattr(bp, "_CRASH_BACKOFF", 0.0)
    monkeypatch.setattr(bp, "_CANCEL_POLL", 0.02)
    yield s
    time.sleep(0.05)  # finally 연쇄 dispatch 데몬 스레드가 다음 테스트의 가드를 덮지 않게.
    bp._reset_running_guard_for_tests()


class _FakeProc:
    """스크립트된 자식 — rc=None 이면 kill_tree 전까지 생존(test_backfill_supervisor 선례)."""

    def __init__(self, rc: int | None) -> None:
        self._rc = rc
        self.killed = False
        self.pid = 9999

    def poll(self):  # noqa: ANN202
        return self._rc

    def wait(self):  # noqa: ANN202
        return self._rc if self._rc is not None else -9

    def kill_tree(self) -> None:
        self.killed = True
        self._rc = -9

    def release(self) -> None:
        pass


def _wait_status(settings: Settings, job_id: str, status: str, timeout: float = 8.0) -> None:
    sm = get_sessionmaker(settings)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with sm() as s:
            row = get_backfill_job(s, job_id)
            if row and row.status == status:
                return
        time.sleep(0.05)
    raise AssertionError(f"상태 {status} 도달 실패(타임아웃)")


def test_dispatch_activates_only_one_at_a_time(settings) -> None:
    """활성 S 자식이 있으면 대기열에 더 있어도 두 번째를 절대 띄우지 않는다."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        j1 = enqueue_segment_job(s, countries="KR", industries="제조")
        j2 = enqueue_segment_job(s, countries="KR", industries="금융")
        s.commit()
        j1_id, j2_id = j1.id, j2.id

    proc = _FakeProc(rc=None)  # 생존형 — 감독 스레드가 running 상태로 유지.
    started = bp.dispatch_next_segment_job(settings, launcher=lambda a, lp: proc)
    assert started == j1_id

    again = bp.dispatch_next_segment_job(
        settings, launcher=lambda a, lp: pytest.fail("두 번째 스폰 금지")
    )
    assert again is None  # _running["S"] 가드 — 두 번째 activate/spawn 없음.
    with sm() as s:
        assert get_backfill_job(s, j2_id).status == QUEUED  # 여전히 대기.

    with sm() as s:  # 정리.
        request_cancel(s, j1_id)
        s.commit()
    _wait_status(settings, j1_id, "cancelled")


def test_done_finishes_and_chains_next_by_priority(settings) -> None:
    """rc=0 + stage=done → DONE 마감, finally 가 우선순위 다음 건을 자동 dispatch 한다."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        j1 = enqueue_segment_job(s, countries="KR", industries="제조", priority=10)
        j2 = enqueue_segment_job(s, countries="KR", industries="금융", priority=20)
        s.commit()
        j1_id, j2_id = j1.id, j2.id

    spawned: list[str] = []

    def launcher(argv, log_path):  # noqa: ANN001
        job_id = argv[argv.index("--job-id") + 1]
        gen = int(argv[argv.index("--job-generation") + 1])
        spawned.append(job_id)
        if job_id == j1_id:
            with sm() as s:  # 자식이 승격 대상 소진을 보고했다고 시뮬레이션.
                record_progress(s, job_id, gen, stage="done", remaining=0)
                s.commit()
        else:
            with sm() as s:  # j2 확인 후 즉시 취소해 테스트를 닫는다.
                request_cancel(s, job_id)
                s.commit()
        return _FakeProc(rc=0)

    started = bp.dispatch_next_segment_job(settings, launcher=launcher)
    assert started == j1_id
    _wait_status(settings, j1_id, DONE)
    _wait_status(settings, j2_id, "cancelled")
    assert spawned == [j1_id, j2_id]  # 우선순위(10 먼저) 순으로 자동 연쇄.
    with sm() as s:
        assert get_backfill_job(s, j1_id).active_track is None


def test_pause_running_transitions_and_chains_next(settings) -> None:
    """running 일시정지 요청 — 취소 분기에서 paused(커서 보존·세대 bump), 다음 건 dispatch."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        j1 = enqueue_segment_job(s, countries="KR", industries="제조", priority=10)
        j2 = enqueue_segment_job(s, countries="KR", industries="금융", priority=20)
        s.commit()
        j1_id, j2_id = j1.id, j2.id

    proc1 = _FakeProc(rc=None)
    proc2 = _FakeProc(rc=None)

    def launcher(argv, log_path):  # noqa: ANN001
        job_id = argv[argv.index("--job-id") + 1]
        gen = int(argv[argv.index("--job-generation") + 1])
        if job_id == j1_id:
            with sm() as s:  # 커서 보존 확인용 진행 시뮬레이션.
                record_progress(s, job_id, gen, cursor="co_123", remaining=5)
                s.commit()
            return proc1
        return proc2

    started = bp.dispatch_next_segment_job(settings, launcher=launcher)
    assert started == j1_id
    time.sleep(0.1)  # record_progress 반영 대기.

    info = bp.request_pause_segment_job(settings, j1_id)
    assert info is not None and info["status"] == RUNNING  # 즉시는 안 바뀜(협조 신호만).

    _wait_status(settings, j1_id, PAUSED)
    with sm() as s:
        row = get_backfill_job(s, j1_id)
        assert row.generation == 1  # pause 가 세대를 bump.
        assert row.promote_cursor == "co_123"  # 커서 보존.
        assert row.active_track is None
    assert proc1.killed is True

    _wait_status(settings, j2_id, "running")  # finally 가 다음 건을 자동 dispatch.
    with sm() as s:
        request_cancel(s, j2_id)
        s.commit()
    _wait_status(settings, j2_id, "cancelled")


def test_pause_queued_immediate(settings) -> None:
    """대기 중(queued) 작업은 자식이 없으므로 즉시 paused 로 전이한다."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        j = enqueue_segment_job(s, countries="KR", industries="제조")
        s.commit()
        jid = j.id

    info = bp.request_pause_segment_job(settings, jid)
    assert info is not None and info["status"] == PAUSED
    with sm() as s:
        row = get_backfill_job(s, jid)
        assert row.status == PAUSED
        assert row.active_track is None


def test_resume_respawns_running_segment_job(settings) -> None:
    """재시작 복구 — running S 잔존을 KeyError 없이 세대 bump 재스폰한다."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        j = enqueue_segment_job(s, countries="KR", industries="제조")
        s.commit()
        jid = j.id
    with sm() as s:
        activate_segment_job(s, jid)  # 이전 서버가 남긴 running 잔존 시뮬레이션.
        s.commit()

    spawn_gens: list[str] = []

    def launcher(argv, log_path):  # noqa: ANN001
        spawn_gens.append(argv[argv.index("--job-generation") + 1])
        with sm() as s:
            request_cancel(s, jid)
            s.commit()
        return _FakeProc(rc=0)

    resumed = bp.resume_active_jobs(settings, launcher=launcher)
    assert resumed == 1
    _wait_status(settings, jid, "cancelled")
    assert spawn_gens[0] == "1"  # 세대 bump(0→1, 구세대 보고 펜싱).


def test_resume_dispatches_queued_only_segment_job(settings) -> None:
    """running S 없이 queued 만 있으면 resume 말미 dispatch 가 재개한다."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        j = enqueue_segment_job(s, countries="KR", industries="제조")
        s.commit()
        jid = j.id

    def launcher(argv, log_path):  # noqa: ANN001
        with sm() as s:
            request_cancel(s, jid)
            s.commit()
        return _FakeProc(rc=0)

    resumed = bp.resume_active_jobs(settings, launcher=launcher)
    assert resumed == 0  # running 재개 대상 없음 — dispatch 만 작동.
    _wait_status(settings, jid, "cancelled")


def test_dispatch_none_when_budget_exhausted(settings, monkeypatch) -> None:
    """예산 소진이면 activate/스폰 없이 대기 유지(None)."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        enqueue_segment_job(s, countries="KR", industries="제조")
        s.commit()
    monkeypatch.setattr(bp, "_budget_exhausted", lambda settings, sm: True)

    result = bp.dispatch_next_segment_job(
        settings, launcher=lambda a, lp: pytest.fail("스폰 금지")
    )
    assert result is None


def test_dispatch_none_when_running_guard_held(settings) -> None:
    """``_running['S']`` 가 이미 True 면 대기열이 있어도 None(프로세스 내 이중 가드)."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        enqueue_segment_job(s, countries="KR", industries="제조")
        s.commit()
    with bp._guard:
        bp._running["S"] = True
    try:
        result = bp.dispatch_next_segment_job(
            settings, launcher=lambda a, lp: pytest.fail("스폰 금지")
        )
        assert result is None
    finally:
        with bp._guard:
            bp._running["S"] = False


def test_track_a_c_supervisor_regression(settings) -> None:
    """트랙 S 배선이 A/C 기존 동작을 건드리지 않는다(스모크 — 전체 회귀는 기존 스위트가)."""
    proc = _FakeProc(rc=1)
    job = bp.start_backfill(settings, track="A", launcher=lambda a, lp: proc)
    _wait_status(settings, str(job["id"]), "failed")
    with get_sessionmaker(settings)() as s:
        row = get_backfill_job(s, str(job["id"]))
        assert row.crash_restarts == bp._CRASH_LIMIT


def test_track_a_cancel_path_unchanged(settings) -> None:
    """A/C 취소 경로는 _finish_cancel 통일 후에도 CANCELLED/stop_reason='operator' 그대로."""
    proc = _FakeProc(rc=None)
    job = bp.start_backfill(settings, track="A", launcher=lambda a, lp: proc)
    jid = str(job["id"])
    with get_sessionmaker(settings)() as s:
        request_cancel(s, jid)
        s.commit()
    _wait_status(settings, jid, "cancelled")
    with get_sessionmaker(settings)() as s:
        row = get_backfill_job(s, jid)
        assert row.stop_reason == "operator" and row.active_track is None
    # proc.killed 는 취소가 스폰 전 최상단 체크에 잡히면 False 라 경쟁 의존 — 단언하지 않는다.


def test_resume_after_pause_request_recovers_to_paused(settings) -> None:
    """pause 요청(stop_reason='pause'+cancel_requested) 직후 서버 재시작 → paused 로 복구.

    CANCELLED 로 닫히면 requeue 대상이 아니라 커서째 영구 종료된다(리뷰 HIGH).
    """
    sm = get_sessionmaker(settings)
    with sm() as s:
        j = enqueue_segment_job(s, countries="KR", industries="제조")
        s.commit()
        activate_segment_job(s, j.id)
        s.commit()
        record_progress(s, j.id, 0, cursor="co_77", stage="promote")
        s.commit()
        jid = j.id
    info = bp.request_pause_segment_job(settings, jid)
    assert info["cancel_requested"] is True and info["stop_reason"] == "pause"

    resumed = bp.resume_active_jobs(settings, launcher=lambda a, lp: pytest.fail("스폰 금지"))
    assert resumed == 0
    with sm() as s:
        row = get_backfill_job(s, jid)
        assert row.status == PAUSED and row.promote_cursor == "co_77"
        assert row.active_track is None and row.generation == 1


def test_dispatch_skips_invalidated_candidate(settings) -> None:
    """맨 앞 후보가 SELECT 직후 무효화돼도 다음 후보로 재시도한다."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        j1 = enqueue_segment_job(s, countries="KR", industries="제조", priority=10)
        j2 = enqueue_segment_job(s, countries="KR", industries="금융", priority=20)
        s.commit()
        j1_id, j2_id = j1.id, j2.id

    import leadcrawler.storage.backfill_job as sj

    calls = {"n": 0}
    orig = sj.activate_segment_job

    def flaky_activate(session, job_id):  # noqa: ANN001
        calls["n"] += 1
        if job_id == j1_id:
            sj.pause_backfill_job(session, j1_id)  # 경쟁: 다른 스레드가 방금 pause(커밋됨).
            session.commit()
        return orig(session, job_id)

    mp = pytest.MonkeyPatch()
    mp.setattr(sj, "activate_segment_job", flaky_activate)  # dispatch 는 호출 시점에 import.
    try:
        started = bp.dispatch_next_segment_job(settings, launcher=lambda a, lp: _FakeProc(rc=None))
    finally:
        mp.undo()
    assert started == j2_id and calls["n"] == 2
    with sm() as s:
        assert get_backfill_job(s, j1_id).status == PAUSED
        request_cancel(s, j2_id)
        s.commit()
    _wait_status(settings, j2_id, "cancelled")
