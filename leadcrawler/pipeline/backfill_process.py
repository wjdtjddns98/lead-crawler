"""백필 supervisor — 자식 CLI 프로세스를 세대 단위로 스폰·감독한다(#352 PR④).

서버(FastAPI)에는 이 가벼운 감독 스레드만 산다: Playwright·enrich 는 전부 자식
프로세스(``python -m leadcrawler.cli fill-emails/backfill-resolve-domains --job-id ...``)
안에서 돌고, ``--max-batches`` 정상종료(exit 0)마다 세대를 올려 재기동한다(메모리 리셋
— OOM 실사고의 최종 안전망). 자식 트리는 Windows Job Object(KILL_ON_JOB_CLOSE)에
결속돼 서버가 어떻게 죽든 함께 정리된다(PID kill 금지 — 재사용 오폭).

계약(#352 인수조건):
- 모든 종료 경로(정상/취소/크래시/예산소진)에서 ``finish_backfill_job`` 은 supervisor 가
  호출한다(자식은 안 함).
- 세대 교체는 자식의 실제 종료(wait 반환) **후에만** 일어난다 — 유령 세대 방지.
- 취소 = 플래그 기록 후 Job 종료(회사 단위 커밋이라 강제종료 안전) — 자식의 협조적
  폴링은 보조 수단.
- 서버 재시작 시 ``resume_active_jobs`` 가 running 행을 자동 재개하되 **취소 플래그를
  먼저 재확인**한다(과거 크롤 워치독 취소사고 방어).
- 예산 소진(세대 경계에서 DB 월합계 재조회)이면 ``budget_exhausted`` 로 명시 종료.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from ..config import Settings
from ..logging import get_logger
from ..storage.backfill_job import (
    BUDGET_EXHAUSTED,
    CANCELLED,
    FAILED,
    TERMINAL,
    backfill_job_dict,
    create_backfill_job,
    finish_backfill_job,
    get_backfill_job,
    update_backfill_job,
)
from ..storage.db import get_sessionmaker
from .winjob import create_job_for

log = get_logger("pipeline.backfill_process")

_TRACK_CMD = {"A": "fill-emails", "C": "backfill-resolve-domains"}
_TRACK_DEFAULTS = {  # (batch, workers, interval) — 현행 운영값(cli 기본과 bat 러너 절충).
    "A": (200, 2, 30.0),
    "C": (200, 2, 60.0),
}
_CRASH_LIMIT = 3  # 연속 비정상종료 회로차단(테스트가 monkeypatch).
_CRASH_BACKOFF = 30.0  # 크래시 재기동 백오프 초.
_CANCEL_POLL = 2.0  # 자식 생존 감시 주기(취소 감지 지연 상한).

# 트랙별 감독 스레드 단일화 가드 — DB active_track 유니크가 정본이고, 이건 같은
# 프로세스 안의 이중 스레드 방지(crawl background._guard 선례).
_guard = threading.Lock()
_running: dict[str, bool] = {"A": False, "C": False}


class BackfillProcessError(Exception):
    """스폰/감독 실패(호출자=API 는 500 계열로 변환)."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _default_launcher(argv: list[str], log_path: Path):  # noqa: ANN202
    """실 자식 스폰 — stdout/err 는 로그 파일 append, Job Object 결속(fail-closed).

    반환 객체 계약: ``pid``·``poll()``·``wait()``·``kill_tree()``(트리 전체 종료).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "ab")  # noqa: SIM115 — 자식 수명과 함께 감(프로세스 종료 시 OS 정리).
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv 는 코드가 조립(셸 미경유).
            argv, cwd=str(_repo_root()), stdout=handle, stderr=subprocess.STDOUT
        )
    except Exception:
        handle.close()  # 스폰 실패 시 로그 fd 누수 방지.
        raise
    # ponytail: Popen 직후 Job 배정이라 배정 전 극소 창(자식이 손자를 먼저 낳는 경우)이
    # 있다 — 실제 자식은 DB·락 초기화 뒤에야 Chromium 을 띄워 실질 위험 낮음. 원자 배정이
    # 필요해지면 CREATE_SUSPENDED+Resume 또는 PROC_THREAD_ATTRIBUTE_JOB_LIST 로 승급.
    raw_handle = getattr(proc, "_handle", None)  # CPython 내부 속성 — 부재 시 명시 실패.
    try:
        if raw_handle is None:
            raise OSError("Popen._handle 부재(CPython 내부 변경) — Job 결속 불가")
        job = create_job_for(int(raw_handle))
    except OSError:
        proc.kill()  # fail-closed — Job 없는 자식은 서버 사망 시 고아가 된다.
        proc.wait()
        handle.close()
        raise

    class _Managed:
        pid = proc.pid

        def poll(self):  # noqa: ANN202
            return proc.poll()

        def wait(self):  # noqa: ANN202
            return proc.wait()

        def kill_tree(self) -> None:
            job.terminate()  # Job 안의 자식+Chromium 트리 전체 즉시 종료.
            try:
                # TerminateJobObject 가 조용히 실패해도 감독 스레드가 영원히 안 멈추게
                # 타임아웃 + 직접 kill 최후 방어(2026-08-18 Claude 리뷰 MED-1).
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            handle.close()

        def release(self) -> None:
            job.close()  # 정상종료 후 핸들 정리(트리는 이미 끝났음). 로그 핸들도.
            handle.close()

    return _Managed()


def _budget_exhausted(settings: Settings, sm) -> bool:  # noqa: ANN001
    """월 예산 소진 여부 — DB 월합계를 매번 재조회(프로세스 로컬 캐시 금지 계약)."""
    if not settings.cost_budget_enforce:
        return False
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sm() as s:
        spent = s.execute(
            text("select coalesce(sum(cost_krw), 0) from cost_ledger where month_key = :m"),
            {"m": month},
        ).scalar()
    return int(spent or 0) >= int(settings.monthly_budget_krw)


def _child_argv(job: dict, generation: int) -> list[str]:
    """DB 스냅샷에서만 argv 를 재구성한다(최신 UI 입력·설정으로 바꾸지 않음 — 설계 계약)."""
    track = str(job["track"])
    argv = [
        sys.executable, "-m", "leadcrawler.cli", _TRACK_CMD[track], "--loop",
        "--max-batches", str(job["max_batches"]),
        "--batch", str(job["batch"]),
        "--workers", str(job["workers"]),
        "--min-queue", str(job["min_queue"]),
        "--job-id", str(job["id"]),
        "--job-generation", str(generation),
    ]
    for c in str(job["countries"]).split(","):
        if c.strip():
            argv += ["--country", c.strip()]
    if str(job.get("industries", "")).strip():
        argv += ["--industry", str(job["industries"])]
    if str(job["exclude_industries"]).strip():
        argv += ["--exclude-industry", str(job["exclude_industries"])]
    if job["exclude_listed"]:
        argv += ["--exclude-listed"]
    return argv


def _cancel_requested(sm, job_id: str) -> bool:  # noqa: ANN001
    from ..storage.backfill_job import is_cancel_requested

    with sm() as s:
        return is_cancel_requested(s, job_id)


def _supervise(settings: Settings, job: dict, *, launcher, start_generation: int) -> None:  # noqa: ANN001
    """감독 루프(전용 스레드) — 스폰→감시→세대 교체/종료 판정. 종료 상태 기록은 여기서만."""
    track = str(job["track"])  # dict 키 접근만 try 밖(실패 불가) — 나머지는 전부 안쪽.
    proc = None  # 바깥 예외 핸들러가 살아있는 자식을 정리할 수 있게 루프 밖 참조.
    sm = None
    try:
        # 초기화도 try 안 — get_sessionmaker 등이 던지면 finally 의 _running 해제가
        # 안 돌아 트랙이 영구 박제된다(2026-08-18 Codex 리뷰 HIGH-1).
        sm = get_sessionmaker(settings)
        job_id = str(job["id"])
        generation = start_generation
        crash = 0
        log_path = _repo_root() / "logs" / f"backfill-web-{track}.log"
        while True:
            if _cancel_requested(sm, job_id):
                _finish(sm, job_id, CANCELLED, stop_reason="operator")
                return
            if _budget_exhausted(settings, sm):
                _finish(sm, job_id, BUDGET_EXHAUSTED, stop_reason="monthly_budget")
                return
            try:
                proc = launcher(_child_argv(job, generation), log_path)
            except Exception as exc:  # 스폰 실패 — 재시도 없이 명시 실패(설정/환경 문제).
                _finish(sm, job_id, FAILED, error=f"스폰 실패: {exc}")
                return
            with sm() as s:
                update_backfill_job(s, job_id, pid=proc.pid, generation=generation)
                s.commit()
            log.info("backfill.spawn", track=track, job=job_id, gen=generation, pid=proc.pid)

            # 자식 감시 — 취소가 오면 Job 트리를 즉시 종료(협조 폴링은 보조).
            cancelled = False
            while proc.poll() is None:
                if _cancel_requested(sm, job_id):
                    cancelled = True
                    proc.kill_tree()
                    break
                time.sleep(_CANCEL_POLL)
            if cancelled:
                _finish(sm, job_id, CANCELLED, stop_reason="operator")
                return
            rc = proc.wait()
            proc.release()
            proc = None  # 이 세대는 종료·정리 완료 — 예외 핸들러의 이중 정리 방지.

            if rc == 0:  # max_batches 정상종료 — 세대 교체(메모리 리셋) 후 즉시 재기동.
                crash = 0
                generation += 1
                with sm() as s:
                    update_backfill_job(
                        s, job_id, generation=generation,
                        recycles=int(job.get("recycles", 0)) + 1,
                    )
                    s.commit()
                job["recycles"] = int(job.get("recycles", 0)) + 1
                continue
            crash += 1  # 비정상종료 — 백오프 후 재시도, 연속 한계 도달 시 회로차단.
            with sm() as s:
                update_backfill_job(s, job_id, crash_restarts=crash)
                s.commit()
            log.info("backfill.crash", track=track, job=job_id, rc=rc, crash=crash)
            if crash >= _CRASH_LIMIT:
                if _cancel_requested(sm, job_id):
                    # 마지막 크래시와 취소가 겹치면 사용자 의도(취소)로 기록한다.
                    _finish(sm, job_id, CANCELLED, stop_reason="operator")
                    return
                _finish(
                    sm, job_id, FAILED,
                    error=(
                        f"연속 {crash}회 비정상종료(rc={rc})"
                        + (" — rc=1 반복이면 트랙 잠금 점유(구버전 bat 러너 미재시작)"
                           " 여부 확인" if rc == 1 else "")
                        + (" — rc=86 은 배치 진행 정체 자동 종료(Playwright 행,"
                           " --stall-exit-secs 참고)" if rc == 86 else "")
                    ),
                )
                return
            time.sleep(_CRASH_BACKOFF)
    except Exception as exc:  # 감독 스레드 자체 예외 — 잡을 실패로 닫는다(running 박제 방지).
        jid = str(job.get("id", ""))
        log.info("backfill.supervisor.error", track=track, job=jid, err=str(exc))
        if proc is not None and proc.poll() is None:
            # 살아있는 자식을 반드시 정리 — 감독 없는 자식이 과금 작업을 계속하는
            # 고아 시나리오 차단(2026-08-18 Claude 리뷰 HIGH-2, 핵심 계약).
            try:
                proc.kill_tree()
            except Exception:
                log.info("backfill.orphan_kill.error", track=track, job=jid)
        if sm is not None and jid:
            # _finish 마저 실패하면 잡을 여기서 더 못 닫는다 — running 잔존은 서버
            # 재시작의 resume 경로가 수습한다(ponytail: 이중 실패는 재시작 수습으로).
            try:
                _finish(sm, jid, FAILED, error=f"supervisor 예외: {exc}")
            except Exception:
                log.info("backfill.finish.error", track=track, job=jid)
    finally:
        with _guard:
            _running[track] = False


def _finish(sm, job_id: str, status: str, *, stop_reason=None, error=None) -> None:  # noqa: ANN001
    with sm() as s:
        finish_backfill_job(s, job_id, status, stop_reason=stop_reason, error=error)
        s.commit()


def start_backfill(
    settings: Settings,
    *,
    track: str,
    countries: str = "",
    industries: str = "",
    exclude_industries: str = "",
    exclude_listed: bool = False,
    max_batches: int = 20,
    min_queue: int = 1,
    initial_target: int = 0,
    triggered_by: str | None = None,
    launcher=None,  # noqa: ANN001 — 테스트 주입점(기본=실 Popen+Job Object).
) -> dict[str, object]:
    """백필 작업을 만들고 감독 스레드를 띄운다 — 즉시 job DTO 반환(API 202 용).

    활성 중복은 ``create_backfill_job`` 의 DB 유니크(BackfillBusy)가 정본으로 막고,
    ``_running`` 은 같은 프로세스 내 이중 스레드 방지 보조 가드다.
    """
    batch, workers, _interval = _TRACK_DEFAULTS[track]
    sm = get_sessionmaker(settings)
    with _guard:
        if _running.get(track):
            from ..storage.backfill_job import BackfillBusy

            raise BackfillBusy(track)
        _running[track] = True
    try:
        with sm() as s:
            row = create_backfill_job(
                session=s, track=track, countries=countries, industries=industries,
                exclude_industries=exclude_industries, exclude_listed=exclude_listed,
                batch=batch, workers=workers, max_batches=max_batches, min_queue=min_queue,
                initial_target=initial_target, triggered_by=triggered_by,
            )
            s.commit()
            job = backfill_job_dict(row)
    except BaseException:
        with _guard:
            _running[track] = False
        raise
    _spawn_supervisor(settings, job, launcher=launcher, start_generation=0)
    return job


def _spawn_supervisor(settings: Settings, job: dict, *, launcher, start_generation: int) -> None:  # noqa: ANN001
    """감독 스레드 기동 — 실패 시 잡을 FAILED 로 닫고 가드 원복 후 재전파.

    기동 실패를 안 닫으면 DB active_track + _running 이중 박제로 그 트랙이 서버 재시작
    전까지 영구 BackfillBusy 가 된다(2026-08-18 Claude 리뷰 HIGH-1, background.py 선례).
    """
    track = str(job["track"])
    try:
        t = threading.Thread(
            target=_supervise,
            args=(settings, job),
            kwargs={
                "launcher": launcher or _default_launcher,
                "start_generation": start_generation,
            },
            name=f"backfill-{track}",
            daemon=True,
        )
        t.start()
    except Exception as exc:
        sm = get_sessionmaker(settings)
        _finish(sm, str(job["id"]), FAILED, error=f"감독 스레드 기동 실패: {exc}")
        with _guard:
            _running[track] = False
        raise


def resume_active_jobs(settings: Settings, *, launcher=None) -> int:  # noqa: ANN001
    """서버 기동 시 running 잔존 작업 재개 — 취소 플래그 우선 재확인(취소사고 방어).

    이전 서버의 자식 트리는 Job Object 로 이미 소멸했으므로, 세대를 올려 새로 스폰한다.
    취소가 걸려 있던 작업은 재기동하지 않고 cancelled 로 닫는다. 재개 건수 반환.
    """
    from ..schema import BackfillJobRow
    from sqlalchemy import select

    sm = get_sessionmaker(settings)
    resumed = 0
    with sm() as s:
        rows = list(
            s.scalars(select(BackfillJobRow).where(BackfillJobRow.active_track.is_not(None)))
        )
        jobs = [backfill_job_dict(r) for r in rows if r.status not in TERMINAL]
    for job in jobs:
        job_id = str(job["id"])
        track = str(job["track"])
        if job["cancel_requested"]:
            _finish(sm, job_id, CANCELLED, stop_reason="cancelled_before_resume")
            continue
        with _guard:
            if _running.get(track):
                continue  # 같은 프로세스에 이미 감독 스레드 존재(이례) — 중복 스폰 금지.
            _running[track] = True
        # 세대 bump 는 가드 통과 **후** — 중복 호출이 이미 도는 자식의 보고를 펜싱으로
        # 유실시키지 않게(2026-08-18 Codex 리뷰 MED-8).
        next_gen = int(job["generation"]) + 1  # 이전 서버 세대와 구분(구세대 보고 펜싱).
        with sm() as s:
            update_backfill_job(s, job_id, generation=next_gen)
            s.commit()
        _spawn_supervisor(settings, job, launcher=launcher, start_generation=next_gen)
        resumed += 1
        log.info("backfill.resume", track=track, job=job_id, gen=next_gen)
    return resumed


def backfill_status(settings: Settings, track: str) -> dict[str, object] | None:
    """트랙 최신 작업 DTO(없으면 None) — API 폴링용 읽기 헬퍼."""
    from ..storage.backfill_job import latest_backfill_job

    sm = get_sessionmaker(settings)
    with sm() as s:
        row = latest_backfill_job(s, track)
        return backfill_job_dict(row) if row else None


def request_stop(settings: Settings, job_id: str) -> dict[str, object] | None:
    """중지 요청 — 취소 플래그만 기록(감독 스레드가 Job 트리 종료·상태 마감).

    감독 스레드가 없어도(서버 재시작 직후 미재개 등) 플래그는 남아 재개 시 처리된다.
    """
    from ..storage.backfill_job import request_cancel

    sm = get_sessionmaker(settings)
    with sm() as s:
        row = request_cancel(s, job_id)
        s.commit()
        return backfill_job_dict(row) if row else None


def _reset_running_guard_for_tests() -> None:
    """테스트 격리용 — 모듈 전역 가드 초기화."""
    with _guard:
        _running["A"] = False
        _running["C"] = False


def get_backfill_job_dict(settings: Settings, job_id: str) -> dict[str, object] | None:
    """단건 조회 DTO(없으면 None)."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        row = get_backfill_job(s, job_id)
        return backfill_job_dict(row) if row else None
