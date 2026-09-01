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

import os
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
    DONE,
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

_TRACK_CMD = {"A": "fill-emails", "C": "backfill-resolve-domains", "S": "segment-run"}
_S_STALL_EXIT_S = 900.0  # 트랙 S 자식의 --stall-exit-secs(정체 자기종료) 기본값.
_TRACK_DEFAULTS = {  # (batch, workers, interval) — 현행 운영값(cli 기본과 bat 러너 절충).
    "A": (200, 2, 30.0),
    "C": (200, 2, 60.0),
    # S: batch/workers/max_batches 는 job 행(enqueue_segment_job 기본값)에서 오므로 여기선
}
_CRASH_LIMIT = 3  # 연속 비정상종료 회로차단(테스트가 monkeypatch).
_CRASH_BACKOFF = 30.0  # 크래시 재기동 백오프 초.
_CANCEL_POLL = 2.0  # 자식 생존 감시 주기(취소 감지 지연 상한).

# 트랙별 감독 스레드 단일화 가드 — DB active_track 유니크가 정본이고, 이건 같은
# 프로세스 안의 이중 스레드 방지(crawl background._guard 선례).
_guard = threading.Lock()
_running: dict[str, bool] = {"A": False, "C": False, "S": False}


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
        # PYTHONUTF8 강제: 자식 stdout 이 파일이라 로케일(cp949) 인코딩을 쓰는데, 한국어
        # 메시지의 em dash(—) 가 cp949 에 없어 완료 echo 에서 UnicodeEncodeError→rc=1 →
        # 감독자가 크래시로 오판(2026-08-31 실사고 — 서버가 UTF-8 없이 기동됐을 때).
        env = {**os.environ, "PYTHONUTF8": "1"}
        proc = subprocess.Popen(  # noqa: S603 — argv 는 코드가 조립(셸 미경유).
            argv, cwd=str(_repo_root()), stdout=handle, stderr=subprocess.STDOUT, env=env
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
    if track == "S":
        # 필터(countries/industries/listed/regions)는 자식이 job 행에서 직접 로드한다
        # (설계 §3) — argv 에는 실행 파라미터만 싣는다.
        return [
            sys.executable, "-m", "leadcrawler.cli", _TRACK_CMD[track],
            "--job-id", str(job["id"]),
            "--job-generation", str(generation),
            "--batch", str(job["batch"]),
            "--workers", str(job["workers"]),
            "--max-batches", str(job["max_batches"]),
            "--stall-exit-secs", str(_S_STALL_EXIT_S),
        ]
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
                _finish_cancel(sm, track, job_id)
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
            # ponytail: 트랙 S 발견 단계의 progress_at 정체 감시는 두지 않는다 — 발견은 세그먼트
            # 단위 블로킹이라 신규 0건 구간이 정상적으로 1시간을 넘을 수 있고(설계 §7 "발견
            # 단계 코드 제한 없음"), 재시작 직후 오래된 progress_at 으로 즉시 오탐·상한 없는
            # kill 루프가 실증됐다(리뷰 HIGH). 승격 단계는 자식 자체 워치독(rc 86)이 크래시
            # 회로차단을 탄다. 취소/pause 는 발견 중에도 세그먼트 경계에서 협조 중단된다.
            cancelled = False
            while proc.poll() is None:
                if _cancel_requested(sm, job_id):
                    cancelled = True
                    proc.kill_tree()
                    break
                time.sleep(_CANCEL_POLL)
            if cancelled:
                _finish_cancel(sm, track, job_id)
                return
            rc = proc.wait()
            proc.release()
            proc = None  # 이 세대는 종료·정리 완료 — 예외 핸들러의 이중 정리 방지.

            if rc == 0:  # max_batches 정상종료 — 세대 교체(메모리 리셋) 후 즉시 재기동.
                if track == "S":
                    with sm() as s:
                        row_now = get_backfill_job(s, job_id)
                    if row_now is not None and row_now.stage == "done":
                        _finish(sm, job_id, DONE)
                        _enqueue_repeat(sm, job_id)  # 반복 잡이면 다음 회차 적재(finally 의 dispatch 는
                        return  # not_before 미래라 건너뛰고, 큐 티커가 시각에 맞춰 시작한다).
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
                    _finish_cancel(sm, track, job_id)
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
        if track == "S":
            # 이 잡의 슬롯이 막 비었다 — 대기열 다음 건을 바로 이어 돌린다(설계 §3).
            try:
                dispatch_next_segment_job(settings, launcher=launcher)
            except Exception:
                log.info("backfill.dispatch.error", track=track)


def _finish(sm, job_id: str, status: str, *, stop_reason=None, error=None) -> None:  # noqa: ANN001
    with sm() as s:
        finish_backfill_job(s, job_id, status, stop_reason=stop_reason, error=error)
        s.commit()


def _enqueue_repeat(sm, job_id: str) -> str | None:  # noqa: ANN001
    """done 반복 잡의 다음 회차 적재 — 실패해도 감독 종료를 막지 않는다(로그만)."""
    from ..storage.backfill_job import enqueue_repeat_of

    try:
        with sm() as s:
            row = enqueue_repeat_of(s, job_id)
            s.commit()
            if row is not None:
                log.info("segment.repeat.enqueued", job=job_id, next=row.id,
                         not_before=row.not_before.isoformat() if row.not_before else None)
                return row.id
    except Exception as exc:  # pragma: no cover — 적재 실패는 가시화만(반복 체인 중단).
        log.warning("segment.repeat.enqueue_error", job=job_id, err=str(exc))
    return None


_ticker_started = False


def segment_ticker_tick(settings: Settings, *, launcher=None) -> str | None:  # noqa: ANN001
    """큐 티커 1틱 — 실행 가능 시각이 된 대기 잡을 디스패치한다(활성 S 있으면 no-op)."""
    try:
        return dispatch_next_segment_job(settings, launcher=launcher)
    except Exception as exc:  # 틱 예외는 삼킨다 — 스레드가 죽으면 반복이 영구 정지.
        log.warning("segment.ticker.error", err=str(exc))
        return None


def start_segment_ticker(settings: Settings) -> bool:
    """서버 기동 시 큐 티커 데몬 스레드 시작(프로세스당 1회, dry_run 은 no-op).

    반복 복제분(not_before 미래)은 이벤트(잡 종료·생성)로는 시작되지 않으므로 주기 폴링이
    필요하다. 재기동 후에도 티커가 다시 뜨므로 대기 중이던 회차는 자동 재개된다.
    ponytail: APScheduler 대신 sleep 루프 — 잡 1종·간격 1개.
    """
    global _ticker_started
    if settings.dry_run or _ticker_started:
        return False
    _ticker_started = True

    def _loop() -> None:
        while True:
            time.sleep(max(5, int(settings.segment_ticker_interval_sec)))
            segment_ticker_tick(settings)

    threading.Thread(target=_loop, name="segment-ticker", daemon=True).start()
    return True


def _finish_cancel(sm, track: str, job_id: str) -> None:  # noqa: ANN001
    """취소 종료 판정 — 트랙 S 이고 pause 요청(``stop_reason='pause'``)이면 paused 로 닫고,
    아니면 기존 CANCELLED 로 닫는다(설계 §3 취소 분기)."""
    if track == "S":
        from ..storage.backfill_job import pause_backfill_job

        with sm() as s:
            row = get_backfill_job(s, job_id)
            if row is not None and row.stop_reason == "pause":
                paused = pause_backfill_job(s, job_id)
                s.commit()
                if paused is not None:
                    return
                # 전이 실패(이미 종료 등)면 아래 CANCELLED 로 폴백 — running+active_track 점유
                # 상태로 남아 대기열이 멈추는 무증상 데드락 방지(리뷰 MED).
    _finish(sm, job_id, CANCELLED, stop_reason="operator")


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
            # 트랙 S pause 요청(stop_reason='pause') 중 재시작이면 paused 로 복구 — CANCELLED 로
            # 닫으면 requeue 대상이 아니라 커서째 영구 종료된다(리뷰 HIGH). 그 외는 기존대로.
            if track == "S":
                _finish_cancel(sm, track, job_id)
            else:
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
    # 트랙 S: running 재개가 없었던 경우(대기열만 남음)도 서버 재시작 후 자동 재개(설계 §3).
    try:
        dispatch_next_segment_job(settings, launcher=launcher)
    except Exception:  # A/C 재개 결과(resumed·로그)를 S 디스패치 실패가 삼키지 않게.
        log.info("backfill.dispatch.error", track="S")
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
        _running["S"] = False


def get_backfill_job_dict(settings: Settings, job_id: str) -> dict[str, object] | None:
    """단건 조회 DTO(없으면 None)."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        row = get_backfill_job(s, job_id)
        return backfill_job_dict(row) if row else None


def dispatch_next_segment_job(settings: Settings, *, launcher=None) -> str | None:  # noqa: ANN001
    """대기열의 다음 세그먼트 작업을 자동 시작한다(공개 — PR5·``_supervise`` finally·
    ``resume_active_jobs`` 말미가 호출). 예산 소진·활성 S 자식 존재·대기열 공백이면 None
    (대기 유지) — 활성 S 자식이 있으면 절대 두 번째를 띄우지 않는다.

    경쟁은 ``activate_segment_job`` 의 ``active_track`` UNIQUE(짧은 단독 트랜잭션) + 프로세스
    내 ``_running`` 가드가 이중 방어한다(설계 §3).
    """
    from ..storage.backfill_job import BackfillBusy, activate_segment_job, next_queued_segment_job

    sm = get_sessionmaker(settings)
    if _budget_exhausted(settings, sm):
        return None
    with _guard:
        if _running.get("S"):
            return None
        _running["S"] = True
    job: dict[str, object] | None = None
    try:
        # 후보가 SELECT→UPDATE 사이에 pause/cancel 로 무효화(rowcount 0)되면 다음 후보로 재시도
        # (상한 3 — 리뷰 MED). BackfillBusy(다른 S 활성)는 정상 케이스라 즉시 포기.
        for _attempt in range(3):
            with sm() as s:
                candidate = next_queued_segment_job(s)
                if candidate is None:
                    break
                try:
                    row = activate_segment_job(s, candidate.id)
                except BackfillBusy:
                    break
                if row is not None:
                    s.commit()
                    job = backfill_job_dict(row)
                    break
    except BaseException:
        with _guard:
            _running["S"] = False
        raise
    if job is None:
        with _guard:
            _running["S"] = False
        return None
    _spawn_supervisor(settings, job, launcher=launcher, start_generation=int(job["generation"]))
    return str(job["id"])


def request_pause_segment_job(settings: Settings, job_id: str) -> dict[str, object] | None:
    """세그먼트 작업 일시정지 요청(PR5 API 가 호출) — 즉시 반영 또는 협조 중단 신호.

    running: ``stop_reason='pause'`` 를 먼저 기록한 뒤 ``request_cancel``(cancel_requested=True)
    — 실제 paused 전이는 ``_supervise`` 의 취소 분기(자식 종료 확인 후, :func:`_finish_cancel`)
    에서 일어난다(수초 지연, 자식 kill 은 기존 취소 감시 경로). queued: 자식이 없으므로
    ``pause_backfill_job`` 으로 즉시 전이. 그 외 상태(이미 paused·종료건)는 현재 DTO 를
    그대로 반환 — 409 판정은 PR5(API) 몫. 대상 없음(트랙 불일치 포함)이면 None.
    """
    from ..storage.backfill_job import QUEUED, RUNNING, TRACK_S, pause_backfill_job, request_cancel

    sm = get_sessionmaker(settings)
    with sm() as s:
        row = get_backfill_job(s, job_id)
        if row is None or row.track != TRACK_S:
            return None
        if row.status == QUEUED:
            row = pause_backfill_job(s, job_id) or row
            s.commit()
            return backfill_job_dict(row)
        if row.status == RUNNING:
            # running 조건부 원자 UPDATE — 자식이 막 DONE 으로 닫은 행에 'pause' 잔존 방지(리뷰 LOW).
            from sqlalchemy import update as _update

            from ..schema import BackfillJobRow

            s.execute(
                _update(BackfillJobRow)
                .where(BackfillJobRow.id == job_id, BackfillJobRow.status == RUNNING)
                .values(stop_reason="pause")
                .execution_options(synchronize_session=False)
            )
            row = request_cancel(s, job_id) or row
            s.commit()
            s.refresh(row)
            return backfill_job_dict(row)
        return backfill_job_dict(row)
