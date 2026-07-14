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
from ..sources.segments import generate_segments, parse_regions
from ..storage.crawl_job import (
    CANCELLED,
    DONE,
    FAILED,
    MODE_CONTINUOUS,
    MODE_ONCE,
    active_crawl_job,
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
    regions: str = "",
    discovery_only: bool = False,
) -> dict[str, object]:
    """크롤 작업을 만들고 백그라운드 실행을 시작한다 — 작업 스냅샷(dict) 반환.

    이미 진행 중이면 :class:`CrawlBusy`. ``runner`` 주입 시 그것으로 실행(테스트 동기화).
    ``target_count`` >0 이면 실존 저장 누계가 그 값에 도달할 때 조기 종료(0=세그먼트 전부 소진).
    ``continuous`` 면 취소 전까지 라운드를 반복한다(mode='continuous' 로 기록).
    ``regions``('all' 또는 쉼표구분)는 KR 세그먼트를 지역별 검색 세그먼트로 팬아웃한다
    (KR 외 국가는 무시 — :func:`generate_segments`).
    ``target_count``/``regions``/``discovery_only`` 는 crawl_job 행에 스냅샷으로 저장돼
    워치독 재기동이 원 요청 조건을 그대로 복원한다.
    """
    global _running
    # 발견 모드 — 비싼 이메일 escalation(헤드리스·OCR·이메일API·Vision)을 꺼 static 만으로
    # 빠르게 발견·큐적재. 무이메일 회사는 별도 채우기 패스가 나중에 채운다(2단계 분리).
    if discovery_only:
        settings = settings.model_copy(update={
            "enrich_headless": False, "enrich_ocr": False,
            "enrich_email_api": False, "enrich_vision": False,
        })
    inds = [s for s in industries.split(",") if s.strip()]
    ctys = [s for s in countries.split(",") if s.strip()] or None
    segments = generate_segments(
        inds, countries=ctys, listed=[listed], regions=parse_regions(regions)
    )
    if len(segments) > settings.crawl_max_segments:
        raise CrawlTooLarge(
            f"세그먼트 {len(segments)}개가 상한({settings.crawl_max_segments})을 초과합니다. "
            "국가/업종/지역을 좁히세요."
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
                target_count=target_count,
                regions=regions,
                discovery_only=discovery_only,
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
        # discovery_only 크롤은 이메일을 미루므로, 같은 트리거로 채우기 consumer 를 병행 스폰한다
        # (웹앱 '크롤 실행' 한 번 = 발견+이메일 병렬). runner 주입(테스트)·dry_run 은 스폰 안 함.
        if discovery_only and runner is None and not settings.dry_run:
            _spawn_consumer_thread(settings, job_id)
        # 도메인 백필 consumer — discovery_only 여부·국가 무관, persist 크롤이면 항상 병행.
        # 발견 시점 inline 해석(run.py)이 실패/스킵한(쿼터 소진 등) 회사를 재시도해 승격
        # 기회를 살린다. 웹앱 '크롤 실행' 버튼 하나로 발견+이메일+도메인재시도가 전부
        # 자동으로 돈다 — 사용자가 별도 CLI(backfill-resolve-domains 등)를 몰라도 된다.
        # resolve_domains opt-in 게이트 — CLI(cli.py)·run.py 인라인 해석과 동일하게 지켜야
        # 한다(2026-07-10 적대 리뷰 MED-2: 게이트 없으면 opt-in 안 한 운영자도 persist 크롤마다
        # 유료 검색키가 있으면 과금이 도는 사고).
        if persist and runner is None and not settings.dry_run and settings.resolve_domains:
            _spawn_domain_backfill_thread(settings, job_id)
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


# 이메일 채우기 consumer(companion) 상수 — 발견 스레드와 경합하지 않게 보수적.
_FILL_MIN_QUEUE = 20   # 무이메일 대상이 이 수 이상일 때만 배치(그 전엔 대기 — 배치 효율).
_FILL_BATCH = 200      # 배치당 최대 회사 수.
_FILL_IDLE_SEC = 15.0  # 대상 부족/실패 시 폴링 대기.


def _spawn_consumer_thread(settings: Settings, job_id: str) -> None:
    """발견 크롤과 병행하는 이메일 채우기 consumer 데몬을 띄운다(discovery_only 크롤 전용).

    발견(producer)이 회사+홈페이지를 큐에 쌓는 동안, 이 consumer 가 '실존·무이메일' 회사를
    배치 병렬로 잡아 헤드리스/OCR 로 이메일을 채운다. 크롤 잡 생명주기에 결속 — 크롤이
    취소되거나(``is_cancel_requested``) 더 이상 active 가 아니면 함께 종료한다. 워커는 발견보다
    낮게(헤드리스 경합 방지). 웹앱 '크롤 실행' 한 번으로 발견+이메일이 병렬로 돈다.
    """
    def _run() -> None:
        from .fill import count_targets, fill_batch  # 지연 import(순환 회피).

        sm = get_sessionmaker(settings)
        workers = max(2, settings.enrich_workers // 3)  # 발견 16 → consumer ~5(경합 방지).
        log.info("consumer.start", job=job_id, workers=workers)
        while not _read_cancel(sm, job_id):
            session = sm()  # 크롤이 끝났으면(active 아님/다른 잡) consumer 도 종료.
            try:
                active = active_crawl_job(session)
            finally:
                session.close()
            if active is None or active.id != job_id:
                break
            if count_targets(sm) < _FILL_MIN_QUEUE:  # 임계 미만 → 더 쌓일 때까지 대기.
                time.sleep(_FILL_IDLE_SEC)
                continue
            try:
                fill_batch(settings, sm, limit=_FILL_BATCH, workers=workers)
            except Exception as exc:  # 배치 실패가 consumer 를 안 죽이게(폭주 방지 대기).
                log.info("consumer.batch.error", job=job_id, err=str(exc))
                time.sleep(_FILL_IDLE_SEC)
        log.info("consumer.stop", job=job_id)

    threading.Thread(target=_run, name=f"fill-{job_id}", daemon=True).start()


# 도메인 백필 consumer 상수 — 검색 API(Naver/CSE/Serper) 라 이메일 채우기보다 보수적.
_RESOLVE_MIN_QUEUE = 20
_RESOLVE_BATCH = 100
_RESOLVE_IDLE_SEC = 30.0


def _spawn_domain_backfill_thread(settings: Settings, job_id: str) -> None:
    """크롤과 병행하는 도메인 재해석 consumer 데몬을 띄운다(전세계, discovery_only 무관).

    발견 시점 inline 도메인 해석(``run.py`` — 도메인 없는 기업을 회사명으로 1회 시도)이
    쿼터 소진 등으로 실패하면 그 회사는 dedup 원장에 이름 키로만 남아 재크롤로도
    다시 안 잡힌다(제약①). 이 consumer 가 그 정체분을 주기적으로 되짚어 재시도한다.
    크롤 잡 생명주기에 결속(취소·종료 시 함께 종료) — ``_spawn_consumer_thread`` 와
    동일 관례. 워커는 발견/이메일채우기보다 낮게(검색 API 레이트 보수적).
    """
    def _run() -> None:
        from .fill import count_resolve_targets, resolve_batch  # 지연 import(순환 회피).

        sm = get_sessionmaker(settings)
        workers = max(1, settings.enrich_workers // 3)
        log.info("resolve_backfill.start", job=job_id, workers=workers)
        while not _read_cancel(sm, job_id):
            session = sm()
            try:
                active = active_crawl_job(session)
            finally:
                session.close()
            if active is None or active.id != job_id:
                break
            if count_resolve_targets(sm) < _RESOLVE_MIN_QUEUE:
                time.sleep(_RESOLVE_IDLE_SEC)
                continue
            try:
                resolve_batch(settings, sm, limit=_RESOLVE_BATCH, workers=workers)
            except Exception as exc:  # 배치 실패가 consumer 를 안 죽이게(폭주 방지 대기).
                log.info("resolve_backfill.batch.error", job=job_id, err=str(exc))
                time.sleep(_RESOLVE_IDLE_SEC)
        log.info("resolve_backfill.stop", job=job_id)

    threading.Thread(target=_run, name=f"resolve-{job_id}", daemon=True).start()


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
    # 카운터 throttle 상태 — 마지막 DB 반영 시각·최근 누계 카운터·현 라운드 원시값.
    # base = 완료된 라운드들의 discovered/enriched/saved 누계(연속 크롤에서 마지막 라운드만
    # 보이던 버그 수정 — 라운드마다 run_pipeline 이 counts 를 0 으로 새로 시작하므로 여기서 합산).
    state: dict[str, object] = {"last": 0.0, "counts": None, "round": None}
    base = {"discovered": 0, "enriched": 0, "saved": 0}

    def _cumulative(counts: dict[str, int]) -> dict[str, int]:
        # discovered/enriched/saved 는 완료 라운드 합(base)+현 라운드분(누계). segments_* 는
        # 현 라운드값 그대로(진행바는 라운드 단위). saved=실존 저장=전체큐 적재분이라 누계가
        # 곧 '전체큐에 적재한 총수'.
        merged = dict(counts)
        for k in base:
            merged[k] = base[k] + int(counts.get(k, 0))
        return merged

    def _on_progress(counts: dict[str, int]) -> None:
        state["round"] = counts  # 현 라운드 원시값(라운드 종료 후 base 에 접기용).
        state["counts"] = _cumulative(counts)
        now = time.monotonic()
        if now - float(state["last"]) >= _PROGRESS_THROTTLE_SEC:
            state["last"] = now
            _write_progress(sm, job_id, state["counts"])  # type: ignore[arg-type]

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
            rc = state.get("round")  # 이 라운드 원시 카운터를 base 에 접어 다음 라운드가 누계로 이어감.
            if isinstance(rc, dict):
                for k in base:
                    base[k] += int(rc.get(k, 0))
                state["round"] = None
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


# --- 워치독 --------------------------------------------------------------
# running 잡인데 그 잡의 실행 스레드(crawl-<id>)가 이 프로세스에 없으면 스레드 소멸로 보고
# 죽은 잡을 정리·재기동한다. 크롤 스레드가 except 로 못 잡는 경로(BaseException·OS 강제소멸)로
# 죽어 status='running' 박제되고 24시간 방치되던 실사고(2026-07-08, py-spy 로 스레드 소멸
# 실측) 방어. **DB 하트비트가 아니라 인프로세스 스레드 생존으로 판정** — 워치독과 크롤이 같은
# uvicorn 프로세스라 스레드 생존은 결정적으로 관측 가능하다. 병렬 발견 단계(무-emit 윈도)가
# 300s 넘게 길어도 살아있는 크롤을 오탐 reap 하지 않는다(DB-하트비트 방식의 치명 오탐 회피).
# 서버 프로세스당 1개만 돈다. 서버 재시작(크로스 프로세스)으로 남은 잔재도 새 프로세스엔
# 스레드가 없으니 동일하게 정리·재기동된다.
_watchdog_started = False
_watchdog_lock = threading.Lock()


def _crawl_thread_alive(job_id: str) -> bool:
    """이 프로세스에 job_id 를 도는 크롤 실행 스레드(_spawn_thread 명명)가 살아있는지."""
    name = f"crawl-{job_id}"
    return any(t.name == name and t.is_alive() for t in threading.enumerate())


def start_watchdog(settings: Settings) -> bool:
    """워치독 데몬 스레드를 1회 시작한다(중복 시작·비활성·dry_run 은 no-op). True=시작함."""
    if not settings.crawl_watchdog_enabled or settings.dry_run:
        return False
    global _watchdog_started
    with _watchdog_lock:
        if _watchdog_started:
            return False
        _watchdog_started = True
    threading.Thread(
        target=_watchdog_loop, args=(settings,), name="crawl-watchdog", daemon=True
    ).start()
    log.info("crawl_watchdog.start", interval_sec=settings.crawl_watchdog_interval_sec)
    return True


def _watchdog_loop(settings: Settings) -> None:
    """interval 마다 _watchdog_tick — tick 예외는 삼켜 워치독 자체는 죽지 않는다.

    ``misses`` 는 job_id 별 연속 스레드-부재 횟수(grace 판정용) — 루프가 유지한다.
    """
    misses: dict[str, int] = {}
    while True:
        time.sleep(settings.crawl_watchdog_interval_sec)
        try:
            _watchdog_tick(settings, misses=misses)
        except Exception as exc:  # 워치독은 절대 죽으면 안 된다(자기 자신은 감시 불가).
            log.warning("crawl_watchdog.tick_failed", err=str(exc))


def _watchdog_tick(
    settings: Settings,
    *,
    runner: JobRunner | None = None,
    misses: dict[str, int] | None = None,
    alive_check: Callable[[str], bool] | None = None,
) -> bool:
    """running 잡의 실행 스레드 생존을 검사해 소멸분을 정리·재기동한다. True=수확(reap)함.

    ``runner``=재기동을 테스트에서 동기 실행으로 주입. ``misses``=연속 부재 카운터(grace).
    ``alive_check``=스레드 생존 판정 주입(테스트용, 기본 :func:`_crawl_thread_alive`).
    """
    global _running
    is_alive = alive_check or _crawl_thread_alive
    sm = get_sessionmaker(settings)
    session = sm()
    try:
        row = active_crawl_job(session)
        if row is None:
            return False
        snap = crawl_job_dict(row)  # 세션 닫히기 전에 재기동 파라미터를 스냅샷.
    finally:
        session.close()
    job_id = str(snap["id"])
    if is_alive(job_id):
        if misses is not None:
            misses.pop(job_id, None)  # 살아있음 — 미스 카운터 리셋.
        return False
    # 스레드 부재 — create_crawl_job 과 thread.start() 사이 마이크로갭 오탐을 grace 로 흡수:
    # 연속 grace_misses 회 부재해야 소멸 확정(정상 기동 중인 잡을 잘못 reap 하지 않는다).
    if misses is not None:
        misses[job_id] = misses.get(job_id, 0) + 1
        if misses[job_id] < settings.crawl_watchdog_grace_misses:
            return False
        misses.pop(job_id, None)
    session = sm()
    try:
        fail_running_jobs(session, "워치독: 실행 스레드 소멸 감지(status=running 박제 정리)")
        session.commit()
    finally:
        session.close()
    # 스레드가 finally 를 못 타고 죽었으면 _running=True 로 가드를 점유 중이다(재기동 시 CrawlBusy).
    # 스레드 부재가 확정됐으므로 가드를 강제 해제한다.
    with _guard:
        _running = False
    log.warning("crawl_watchdog.reaped", job=job_id, mode=snap.get("mode"))
    if snap.get("mode") == MODE_CONTINUOUS:
        try:
            # 실행 조건 스냅샷 전체 복원 — 미복원 시 지역한정→전국 확대·target_count 상한
            # 소멸·discovery_only→풀 enrich 로 범위/비용이 원 요청과 달라진다(전수리뷰 HIGH).
            # target_count 는 라운드 기준 상한이라 재기동 라운드에 그대로 적용한다.
            trigger_crawl_job(
                settings,
                countries=str(snap.get("countries") or ""),
                industries=str(snap.get("industries") or ""),
                listed=str(snap.get("listed") or "unknown"),
                persist=bool(snap.get("persist", True)),
                triggered_by="watchdog",
                continuous=True,
                runner=runner,
                target_count=int(snap.get("target_count") or 0),
                regions=str(snap.get("regions") or ""),
                discovery_only=bool(snap.get("discovery_only", False)),
            )
            log.info("crawl_watchdog.restarted", prev=job_id)
        except Exception as exc:  # 재기동 실패해도 정리는 유효(다음 tick·수동 트리거가 이어감).
            log.warning("crawl_watchdog.restart_failed", prev=job_id, err=str(exc))
    return True
