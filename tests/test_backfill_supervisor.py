"""백필 supervisor(#352 PR④) — 세대 재기동·취소·크래시 회로차단·재개(가짜 런처 주입)."""

from __future__ import annotations

import time

import pytest

import leadcrawler.pipeline.backfill_process as bp
from leadcrawler.config import Settings
from leadcrawler.storage.backfill_job import (
    BackfillBusy,
    TERMINAL,
    get_backfill_job,
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
    return s


class _FakeProc:
    """스크립트된 자식 — rc=None 이면 kill_tree 전까지 생존."""

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


def _wait_terminal(settings: Settings, job_id: str, timeout: float = 8.0) -> str:
    sm = get_sessionmaker(settings)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with sm() as s:
            row = get_backfill_job(s, job_id)
            if row and row.status in TERMINAL:
                return row.status
        time.sleep(0.05)
    raise AssertionError("종료 상태 도달 실패(타임아웃)")


def test_recycle_bumps_generation_then_cancel(settings) -> None:
    """정상종료(exit 0)마다 세대·recycles 증가, 취소 플래그로 마감된다."""
    sm = get_sessionmaker(settings)
    spawns: list[int] = []

    def launcher(argv, log_path):  # noqa: ANN001
        spawns.append(1)
        if len(spawns) >= 3:  # 3번째 스폰 전 루프 상단에서 취소가 감지되게 미리 걸어둔다.
            raise AssertionError("취소 후 스폰 금지")
        if len(spawns) == 2:
            with sm() as s:
                request_cancel(s, argv[argv.index("--job-id") + 1])
                s.commit()
        return _FakeProc(rc=0)

    job = bp.start_backfill(settings, track="A", countries="KR", launcher=launcher)
    assert _wait_terminal(settings, str(job["id"])) == "cancelled"
    with sm() as s:
        row = get_backfill_job(s, str(job["id"]))
        assert row.recycles == 2 and row.generation == 2
        assert row.active_track is None
    assert bp._running["A"] is False  # 감독 스레드 가드 해제.


def test_crash_circuit_breaker(settings) -> None:
    """연속 비정상종료 한계 도달 시 failed 로 회로차단(무한 크래시 루프 방지)."""
    job = bp.start_backfill(
        settings, track="A", launcher=lambda argv, log_path: _FakeProc(rc=1)
    )
    assert _wait_terminal(settings, str(job["id"])) == "failed"
    sm = get_sessionmaker(settings)
    with sm() as s:
        row = get_backfill_job(s, str(job["id"]))
        assert row.crash_restarts == bp._CRASH_LIMIT
        assert "비정상종료" in (row.error or "")


def test_cancel_kills_running_child(settings) -> None:
    """실행 중 취소 — 감독이 Job 트리를 죽이고 cancelled 로 마감한다."""
    proc = _FakeProc(rc=None)  # 생존형 자식.
    job = bp.start_backfill(settings, track="C", launcher=lambda a, lp: proc)
    sm = get_sessionmaker(settings)
    time.sleep(0.1)  # 감독이 감시 루프에 진입할 시간.
    with sm() as s:
        request_cancel(s, str(job["id"]))
        s.commit()
    assert _wait_terminal(settings, str(job["id"])) == "cancelled"
    assert proc.killed is True


def test_budget_exhausted_stops_before_spawn(settings, monkeypatch) -> None:
    """예산 소진이면 스폰 없이 budget_exhausted 로 종료한다."""
    monkeypatch.setattr(bp, "_budget_exhausted", lambda settings, sm: True)
    spawned = {"n": 0}

    def launcher(argv, log_path):  # noqa: ANN001
        spawned["n"] += 1
        return _FakeProc(rc=0)

    job = bp.start_backfill(settings, track="A", launcher=launcher)
    assert _wait_terminal(settings, str(job["id"])) == "budget_exhausted"
    assert spawned["n"] == 0


def test_duplicate_start_raises_busy(settings) -> None:
    """같은 트랙 이중 시작은 BackfillBusy(활성 작업이 도는 동안)."""
    proc = _FakeProc(rc=None)
    job = bp.start_backfill(settings, track="A", launcher=lambda a, lp: proc)
    with pytest.raises(BackfillBusy):
        bp.start_backfill(settings, track="A", launcher=lambda a, lp: _FakeProc(rc=0))
    sm = get_sessionmaker(settings)
    with sm() as s:  # 정리 — 취소로 마감.
        request_cancel(s, str(job["id"]))
        s.commit()
    _wait_terminal(settings, str(job["id"]))


def test_resume_bumps_generation_and_respects_cancel(settings) -> None:
    """재개 — 세대를 올려 재스폰하고, 취소돼 있던 작업은 스폰 없이 cancelled 로 닫는다."""
    from leadcrawler.storage.backfill_job import create_backfill_job

    sm = get_sessionmaker(settings)
    with sm() as s:  # 이전 서버가 남긴 running 잔존 2건(A=정상, C=취소 걸림) 시뮬레이션.
        a = create_backfill_job(session=s, track="A")
        c = create_backfill_job(session=s, track="C")
        s.commit()
        a_id, c_id = a.id, c.id
    with sm() as s:
        request_cancel(s, c_id)
        s.commit()

    spawn_gens: list[str] = []

    def launcher(argv, log_path):  # noqa: ANN001
        spawn_gens.append(argv[argv.index("--job-generation") + 1])
        with sm() as s:  # 재개 확인 후 바로 취소해 테스트를 닫는다.
            request_cancel(s, a_id)
            s.commit()
        return _FakeProc(rc=0)

    resumed = bp.resume_active_jobs(settings, launcher=launcher)
    assert resumed == 1  # A 만 재개(C 는 취소 마감).
    assert _wait_terminal(settings, a_id) == "cancelled"
    with sm() as s:
        assert get_backfill_job(s, c_id).status == "cancelled"
        # 재개 스폰은 세대 1(구세대 0 보고 펜싱)·이후 정상 recycle 로 더 오를 수 있다.
        assert get_backfill_job(s, a_id).generation >= 1
    assert spawn_gens[0] == "1"


def test_child_argv_reconstructs_from_snapshot(settings) -> None:
    """argv 는 DB 스냅샷에서만 조립 — 필터·관리 옵션이 정확히 재현된다."""
    job = {
        "id": "bf_x", "track": "A", "max_batches": 20, "batch": 200, "workers": 2,
        "min_queue": 1, "countries": "KR", "exclude_industries": "은행,보험",
        "exclude_listed": True,
    }
    argv = bp._child_argv(job, 3)
    s = " ".join(argv)
    assert "-m leadcrawler.cli fill-emails --loop" in s
    assert "--country KR" in s and "--exclude-industry 은행,보험" in s
    assert "--exclude-listed" in s and "--job-id bf_x" in s and "--job-generation 3" in s


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows Job Object 전용")
def test_windows_job_object_kills_grandchild_tree(tmp_path) -> None:
    """실프로세스: kill_tree 가 자식뿐 아니라 **손자**(Playwright/Chromium 상당)까지 끝낸다."""
    import sys

    beat = tmp_path / "beat.txt"
    # 자식 = 손자(하트비트 기록 루프)를 낳고 대기하는 파이썬 — Chromium 트리 축소 모형.
    inner = f"while True: open(r'{beat}', 'a').write('x'); __import__('time').sleep(0.1)"
    child_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {inner!r}]); "
        "time.sleep(120)"
    )
    proc = bp._default_launcher([sys.executable, "-c", child_code], tmp_path / "job.log")
    end = time.monotonic() + 10  # 손자가 실제로 기록을 시작할 때까지 대기.
    while time.monotonic() < end and not beat.exists():
        time.sleep(0.1)
    assert beat.exists(), "손자 하트비트 시작 실패"
    proc.kill_tree()
    time.sleep(0.5)
    size_after_kill = beat.stat().st_size
    time.sleep(1.0)
    assert beat.stat().st_size == size_after_kill, "kill_tree 후에도 손자가 살아있음"


def test_supervisor_exception_kills_live_child(settings, monkeypatch) -> None:
    """감독 스레드가 자식 생존 중 예외로 죽어도 자식 트리를 정리한다(고아 과금 차단)."""
    proc = _FakeProc(rc=None)  # 생존형 자식.

    def boom(session, job_id, **fields):  # noqa: ANN001
        raise RuntimeError("DB 단절 시뮬레이션")

    monkeypatch.setattr(bp, "update_backfill_job", boom)  # 스폰 직후 pid 기록에서 폭발.
    job = bp.start_backfill(settings, track="A", launcher=lambda a, lp: proc)
    assert _wait_terminal(settings, str(job["id"])) == "failed"
    assert proc.killed is True  # 살아있던 자식이 정리됐다.
    assert bp._running["A"] is False


def test_thread_start_failure_closes_job_and_guard(settings, monkeypatch) -> None:
    """감독 스레드 기동 실패 — 잡 FAILED 마감 + 가드 원복(영구 BackfillBusy 박제 방지)."""
    import threading

    def no_start(self):  # noqa: ANN001
        raise RuntimeError("스레드 고갈 시뮬레이션")

    monkeypatch.setattr(threading.Thread, "start", no_start)
    with pytest.raises(RuntimeError):
        bp.start_backfill(settings, track="A", launcher=lambda a, lp: _FakeProc(rc=0))
    sm = get_sessionmaker(settings)
    with sm() as s:
        from sqlalchemy import select

        from leadcrawler.schema import BackfillJobRow

        row = s.scalars(select(BackfillJobRow).where(BackfillJobRow.track == "A")).first()
        assert row.status == "failed" and row.active_track is None
    assert bp._running["A"] is False
    # 박제가 없으니 즉시 새 시작이 가능해야 한다(스레드 정상 복원 후).
    monkeypatch.undo()


def test_stale_generation_idle_child_self_stops(settings) -> None:
    """idle 구세대 자식 — 세대 교체를 wait 폴링에서 감지해 스스로 멈춘다(잠금 해방)."""
    import leadcrawler.cli as cli
    from leadcrawler.storage.backfill_job import create_backfill_job, update_backfill_job

    sm = get_sessionmaker(settings)
    with sm() as s:
        job = create_backfill_job(session=s, track="A")
        s.commit()
        jid = job.id
    mj = cli._ManagedJob(sm, jid, 0)
    assert mj.wait(0.1) is False  # 유효 세대 — 정상 대기.
    with sm() as s:
        update_backfill_job(s, jid, generation=1)  # supervisor 세대 교체.
        s.commit()
    assert mj.wait(60.0) is True  # 구세대(0)는 즉시 중지 신호를 받는다.
    assert mj.should_stop() is True


def test_supervisor_pre_try_failure_releases_guard(settings, monkeypatch) -> None:
    """감독 스레드 초기화(try 진입 전) 실패도 가드를 원복한다(영구 박제 방지)."""
    from leadcrawler.storage.backfill_job import create_backfill_job

    sm = get_sessionmaker(settings)
    with sm() as s:
        job = create_backfill_job(session=s, track="C")
        s.commit()
        jd = {"track": "C", "id": job.id}

    def boom(_settings):  # noqa: ANN001
        raise RuntimeError("sessionmaker 초기화 실패 시뮬레이션")

    monkeypatch.setattr(bp, "get_sessionmaker", boom)
    with bp._guard:
        bp._running["C"] = True  # start_backfill 이 세팅한 상태 재현.
    bp._supervise(settings, jd, launcher=lambda a, lp: _FakeProc(rc=0), start_generation=0)
    assert bp._running["C"] is False  # finally 가 돌아 가드가 풀렸다.
