"""웹 직접 크롤 — 백그라운드 실행·진행현황·취소.

웹앱 관리자가 '지금 크롤 실행'을 누르면 :func:`trigger_crawl_job` 이 crawl_job 행을
만들고 데몬 스레드에서 :func:`run_pipeline` 을 돌린다(요청은 즉시 202 로 반환). 스레드는
짧은 세션을 열어 카운터를 갱신하고 취소 플래그를 폴링한다(현황은 GET 폴링으로 노출).

연속(continuous) 모드: 취소 전까지 1회전(라운드)을 반복한다 — 24/7 베이스 크롤.
라운드 사이 ``crawl_loop_pause_sec`` 만큼 쉬고, 휴지 중에도 취소를 폴링한다. 진행
카운터는 라운드마다 새로 세고(현재 라운드 기준) ``rounds_done`` 으로 회전수를 노출한다.
등록처 커서가 영속이라 다음 라운드는 이어서 긁는다(같은 구간 재크롤 아님).

동시 1건 제한: 이 프로세스 안에서 크롤은 한 번에 하나만 — uvicorn 단일 프로세스라
모듈 락으로 충분하다. 프로세스가 죽어 남은 stale running 행은 다음 트리거가 정리한다.
스케줄러(별도 프로세스)와의 동시 실행은 막지 않지만, 파이프라인이 기업 단위 트랜잭션·
중복가드로 동시 적재에 안전하다(겹쳐도 데이터 손상 없음).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from ..config import Settings
from ..logging import get_logger
from ..sources.base import Segment
from ..sources.segments import generate_segments
from ..storage.crawl_job import (
    CANCELLED,
    DONE,
    FAILED,
    MODE_CONTINUOUS,
    MODE_ONCE,
    create_crawl_job,
    crawl_job_dict,
    fail_running_jobs,
    is_cancel_requested,
    update_crawl_job,
)
from ..storage.db import get_sessionmaker
from .run import run_pipeline

log = get_logger("crawl_job")

# 동시 1건 가드 — 단일 프로세스(uvicorn) 기준. 스레드가 끝날 때 _running 을 False 로 되돌린다.
_guard = threading.Lock()
_running = False

# 진행 카운터 DB 반영 최소 간격(초) — 기업마다 쓰면 커넥션 churn 이 커서 throttle. 최종
# 카운터는 파이프라인 종료 후 한 번 강제로 확정 기록한다(정확성 보장).
_PROGRESS_THROTTLE_SEC = 1.5

# 취소 플래그 DB 폴링 최소 간격(초) — run_pipeline 이 세그먼트·기업마다 should_cancel() 을
# 부르는데(2만건이면 2만 세션), 취소는 드문 사용자 액션이라 매번 DB 를 칠 필요가 없다. 이
# 간격마다 1회만 읽고 그 사이엔 마지막 값을 돌려준다(세션 churn 제거, 취소 반영은 최대 이만큼 지연).
_CANCEL_POLL_THROTTLE_SEC = 2.0

# 테스트가 동기 실행을 주입할 수 있게 하는 러너 시그니처(기본은 데몬 스레드 spawn).
# (settings, job_id, segments, persist, target_count, continuous)
JobRunner = Callable[[Settings, str, list[Segment], bool, int, bool], None]


class CrawlBusy(RuntimeError):
    """이미 진행 중인 크롤이 있어 새 크롤을 시작할 수 없음(동시 1건 제한)."""


class CrawlTooLarge(ValueError):
    """요청 세그먼트 수가 상한(crawl_max_segments)을 초과 — 우발적 대량 크롤 차단."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_crawl_running() -> bool:
    """이 프로세스에서 크롤 스레드가 도는 중인지(가드 상태)."""
    return _running


def trigger_crawl_job(
    settings: Settings,
    *,
    countries: str,
    industries: str,
    listed: str,
    persist: bool,
    triggered_by: str | None,
    runner: JobRunner | None = None,
    target_count: int = 0,
    continuous: bool = False,
) -> dict[str, object]:
    """크롤 작업을 만들고 백그라운드 실행을 시작한다 — 작업 스냅샷(dict) 반환.

    이미 진행 중이면 :class:`CrawlBusy`. ``runner`` 주입 시 그것으로 실행(테스트 동기화).
    ``target_count`` >0 이면 실존 저장 누계가 그 값에 도달할 때 조기 종료(0=세그먼트 전부 소진).
    ``continuous`` 면 취소 전까지 라운드를 반복한다(mode='continuous' 로 기록).
    """
    global _running
    inds = [s for s in industries.split(",") if s.strip()]
    ctys = [s for s in countries.split(",") if s.strip()] or None
    segments = generate_segments(inds, countries=ctys, listed=[listed])
    if len(segments) > settings.crawl_max_segments:
        raise CrawlTooLarge(
            f"세그먼트 {len(segments)}개가 상한({settings.crawl_max_segments})을 초과합니다. "
            "국가/업종을 좁히세요."
        )

    with _guard:
        if _running:
            raise CrawlBusy("이미 진행 중인 크롤이 있습니다")
        _running = True
    try:
        session = get_sessionmaker(settings)()
        try:
            # _running 이 False 였다 = 이 프로세스에 살아있는 크롤 스레드가 없었다는 뜻.
            # crawl_job 은 웹 트리거 전용이므로 남아있는 running 행은 전부 비정상 종료
            # 잔재다 → 모두 failed 로 정리(다중 잔재 누적·현황 오염 방지).
            fail_running_jobs(session, "중단됨(프로세스 재시작 또는 비정상 종료)")
            row = create_crawl_job(
                session,
                countries=countries,
                industries=industries,
                listed=listed,
                persist=persist,
                segments_total=len(segments),
                triggered_by=triggered_by,
                mode=MODE_CONTINUOUS if continuous else MODE_ONCE,
            )
            info = crawl_job_dict(row)
            session.commit()
        finally:
            session.close()
    except Exception:
        with _guard:  # 행 생성 실패 — 가드 해제(스레드를 못 띄웠으니 직접 되돌린다).
            _running = False
        raise

    job_id = str(info["id"])
    log.info(
        "crawl_job.start",
        job=job_id, countries=countries, industries=industries, continuous=continuous,
    )
    try:
        (runner or _spawn_thread)(settings, job_id, segments, persist, target_count, continuous)
    except Exception as exc:  # 스레드 spawn 실패 — 가드 누수 방지 + 작업을 failed 로.
        log.warning("crawl_job.spawn_failed", job=job_id, err=str(exc))
        _finalize(get_sessionmaker(settings), job_id, status=FAILED, error=f"실행 시작 실패: {exc}")
        with _guard:
            _running = False
        raise
    return info


def _spawn_thread(
    settings: Settings, job_id: str, segments: list[Segment], persist: bool,
    target_count: int = 0, continuous: bool = False,
) -> None:
    """데몬 스레드로 작업을 실행한다(요청 스레드를 막지 않음)."""
    thread = threading.Thread(
        target=run_crawl_job,
        args=(settings, job_id, segments, persist, target_count, continuous),
        name=f"crawl-{job_id}",
        daemon=True,
    )
    thread.start()


def run_crawl_job(
    settings: Settings, job_id: str, segments: list[Segment], persist: bool,
    target_count: int = 0, continuous: bool = False,
) -> None:
    """작업 본체 — 파이프라인을 돌리며 카운터/취소를 DB로 중계하고 종료 상태를 적는다.

    진행/취소/종료는 각각 짧은 세션으로 처리해(읽기 커밋·identity-map stale 회피) 다른
    트랜잭션(취소 요청)이 켠 플래그를 즉시 본다. 예외는 status='failed'+error 로 기록한다.
    ``continuous`` 면 취소가 관측될 때까지 라운드를 반복한다(라운드 사이
    ``crawl_loop_pause_sec`` 휴지 — 휴지 중에도 취소 폴링). 카운터는 라운드마다 새로 센다.
    """
    global _running
    sm = get_sessionmaker(settings)
    # 카운터 throttle 상태 — 마지막 DB 반영 시각과 가장 최근 카운터(종료 후 강제 확정용).
    state: dict[str, object] = {"last": 0.0, "counts": None}

    def _on_progress(counts: dict[str, int]) -> None:
        state["counts"] = counts
        now = time.monotonic()
        if now - float(state["last"]) >= _PROGRESS_THROTTLE_SEC:
            state["last"] = now
            _write_progress(sm, job_id, counts)

    try:
        rounds = 0
        while True:
            run_pipeline(
                segments,
                settings=settings,
                persist=persist,
                on_progress=_on_progress,
                should_cancel=_make_cancel_poller(sm, job_id),
                target_saved=target_count or None,  # 0 → None(세그먼트 전부 소진).
            )
            rounds += 1
            if state["counts"] is not None:  # throttle 로 누락된 최종 카운터를 확정 기록.
                _write_progress(sm, job_id, state["counts"])  # type: ignore[arg-type]
            _write_rounds(sm, job_id, rounds)
            if _read_cancel(sm, job_id):  # 라운드 중 취소 관측 → 즉시 종료(연속 포함).
                _finalize(sm, job_id, status=CANCELLED)
                return
            if not continuous:
                _finalize(sm, job_id, status=DONE)
                return
            if _pause_cancelled(sm, job_id, settings.crawl_loop_pause_sec):
                _finalize(sm, job_id, status=CANCELLED)
                return
    except Exception as exc:  # 크롤 실패 — 작업을 failed 로 남겨 현황에 노출(프로세스는 생존).
        log.warning("crawl_job.failed", job=job_id, err=str(exc))
        _finalize(sm, job_id, status=FAILED, error=str(exc)[:1000])
    finally:
        with _guard:
            _running = False


def _pause_cancelled(sm: sessionmaker, job_id: str, pause_sec: float) -> bool:
    """라운드 사이 휴지 — 휴지 중에도 취소를 폴링한다(True=취소 관측, 즉시 복귀).

    1초 단위로 쪼개 자며 :func:`_make_cancel_poller`(throttle 폴러)로 취소를 본다 —
    긴 휴지 중 취소가 최대 ~throttle 초 안에 반영된다.
    """
    poll = _make_cancel_poller(sm, job_id)
    deadline = time.monotonic() + pause_sec
    while True:
        if poll():
            return True
        remain = deadline - time.monotonic()
        if remain <= 0:
            return False
        time.sleep(min(1.0, remain))


def _write_rounds(sm: sessionmaker, job_id: str, rounds: int) -> None:
    """완료 라운드 수를 기록한다(짧은 세션). 실패해도 크롤은 계속(현황만 손실)."""
    session = sm()
    try:
        update_crawl_job(session, job_id, rounds_done=rounds)
        session.commit()
    except Exception as exc:
        session.rollback()
        log.warning("crawl_job.rounds_failed", job=job_id, err=str(exc))
    finally:
        session.close()


def _write_progress(sm: sessionmaker, job_id: str, counts: dict[str, int]) -> None:
    """카운터 5종을 작업 행에 반영(짧은 세션). 실패해도 크롤은 계속(현황만 손실)."""
    session = sm()
    try:
        update_crawl_job(
            session,
            job_id,
            segments_total=counts["segments_total"],
            segments_done=counts["segments_done"],
            discovered=counts["discovered"],
            enriched=counts["enriched"],
            saved=counts["saved"],
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        log.warning("crawl_job.progress_failed", job=job_id, err=str(exc))
    finally:
        session.close()


def _read_cancel(sm: sessionmaker, job_id: str) -> bool:
    """취소 요청 플래그를 짧은 세션으로 읽는다(stale 회피)."""
    session = sm()
    try:
        return is_cancel_requested(session, job_id)
    finally:
        session.close()


def _make_cancel_poller(
    sm: sessionmaker, job_id: str, *, throttle_sec: float = _CANCEL_POLL_THROTTLE_SEC
) -> Callable[[], bool]:
    """취소 폴링을 throttle 한 should_cancel 콜백을 만든다 — 기업마다 DB 세션 여는 churn 제거.

    ``throttle_sec`` 마다 1회만 DB 를 읽고 사이엔 마지막 값을 돌려준다. 첫 호출은 즉시
    조회한다(시작 전 취소 즉시 반영). 한 번 취소가 관측되면 계속 True(래치) — 취소는 되돌릴
    수 없고 즉시 종료로 가야 하므로 이후 DB 조회도 생략한다. run_pipeline 메인 루프 단일
    스레드만 호출하므로 경합이 없다(워커 스레드는 호출 안 함).
    """
    last_check = float("-inf")  # 첫 호출은 항상 조회(monotonic 기준점 가정 제거 — 시작 전 취소 즉시 반영).
    cancelled = False

    def _poll() -> bool:
        nonlocal last_check, cancelled
        if cancelled:
            return True
        now = time.monotonic()
        if now - last_check < throttle_sec:
            return False
        last_check = now
        if _read_cancel(sm, job_id):
            cancelled = True
            return True
        return False

    return _poll


def _finalize(
    sm: sessionmaker, job_id: str, *, status: str, error: str | None = None
) -> None:
    """종료 상태(done/failed/cancelled)와 finished_at 을 기록한다."""
    session = sm()
    try:
        update_crawl_job(session, job_id, status=status, error=error, finished_at=_now())
        session.commit()
    except Exception as exc:
        session.rollback()
        log.warning("crawl_job.finalize_failed", job=job_id, err=str(exc))
    finally:
        session.close()
