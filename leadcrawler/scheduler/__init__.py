"""24/7 스케줄러 — 매일 크롤 1회전 + Notion 자동 리포팅(일일보고·스크럼·현황).

PO 요구("사람 수작업 0")의 마지막 조각: ``report-auto`` 를 *사람이 호출* 하는 대신,
이 스케줄러가 매일 지정 시각(UTC)에 무인 실행한다.

설계 원칙(프로젝트 공통 패턴):
- **opt-in·기본 off**(``scheduler_enabled``). APScheduler 는 선택적 extra ``schedule`` —
  미설치면 :func:`start_scheduler` 가 안내 메시지와 함께 RuntimeError 로 멈춘다(우발 기동 0).
- 일일 잡 본체 :func:`run_daily_report` 는 APScheduler 에 의존하지 않는 **순수 호출 단위** —
  dry_run 에서 네트워크 없이 결정적으로 동작하고, 단위 테스트가 직접 호출해 검증한다.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ..logging import get_logger
from ..pipeline import run_pipeline
from ..reporting import auto_report
from ..sources.segments import generate_segments

log = get_logger("scheduler")


def _today_utc() -> str:
    """오늘 일자(YYYY-MM-DD, UTC)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_db_target(
    settings: Settings,
) -> tuple[list[str], list[str] | None, str, bool] | None:
    """DB 크롤 타깃을 (업종, 국가|None, listed, persist)로 읽는다(없거나 실패면 None).

    DB 미구성/미마이그레이션/빈 행은 모두 None 으로 폴백시킨다(dry_run·DB없는 환경 안전).
    세션이 열린 동안 값을 평탄화해 detached 접근을 피한다.
    """
    try:
        from ..storage.crawl_target import get_crawl_target
        from ..storage.db import get_sessionmaker

        session = get_sessionmaker(settings)()
        try:
            row = get_crawl_target(session)
            if row is None or not row.industries.strip():
                return None
            inds = [s for s in row.industries.split(",") if s.strip()]
            ctys = [s for s in row.countries.split(",") if s.strip()] or None
            return inds, ctys, (row.listed or "unknown"), row.persist
        finally:
            session.close()
    except Exception as exc:  # DB 없음/미마이그레이션 등 — .env 폴백(운영 가시성 위해 warning).
        log.warning("scheduler.target.db_unavailable", err=str(exc))
        return None


def _effective_target(settings: Settings) -> tuple[list[str], list[str] | None, str, bool]:
    """이번 크롤 타깃 — DB(웹앱 관리자 설정) 우선, 없으면 .env(report_*) 폴백."""
    db = _load_db_target(settings)
    if db is not None:
        return db
    inds = [s for s in settings.report_industries.split(",") if s.strip()] or ["건설"]
    ctys = [s for s in settings.report_countries.split(",") if s.strip()] or None
    return inds, ctys, "unknown", settings.report_persist


def run_daily_report(
    settings: Settings | None = None, *, date: str | None = None
) -> dict[str, dict]:
    """일일 잡 본체 — 설정대로 크롤 1회전 후 Notion 3종 보드를 자동 기입한다.

    APScheduler 잡이 매일 호출하는 단위이자, 테스트가 직접 부르는 진입점.
    ``dry_run`` 기본이라 키 없이도 결정적으로 동작한다.
    """
    settings = settings or get_settings()
    report_date = date or _today_utc()
    inds, ctys, listed, persist = _effective_target(settings)
    segments = generate_segments(inds, countries=ctys, listed=[listed])
    leads = run_pipeline(segments, settings=settings, persist=persist)
    result = auto_report(
        leads, date=report_date, settings=settings, milestone=settings.report_milestone
    )
    log.info("scheduler.daily_done", date=report_date, leads=len(leads))
    return result


def start_scheduler(settings: Settings | None = None) -> None:
    """블로킹 스케줄러를 띄워 매일 지정 시각(UTC)에 :func:`run_daily_report` 를 돈다.

    APScheduler(선택적 extra ``schedule``) 미설치 시 설치 안내와 함께 RuntimeError.
    ``scheduler_enabled=False`` 면 기동을 거부한다(명시적 opt-in).
    """
    settings = settings or get_settings()
    if not settings.scheduler_enabled:
        raise RuntimeError(
            "스케줄러가 비활성입니다. LEADCRAWLER_SCHEDULER_ENABLED=true 로 활성화하세요."
        )
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ModuleNotFoundError as exc:  # 선택적 extra 미설치 — 우발 기동 방지
        raise RuntimeError(
            "APScheduler 가 없습니다. `pip install -e .[schedule]` 로 설치하세요."
        ) from exc

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: run_daily_report(settings),
        CronTrigger(hour=settings.report_hour, minute=settings.report_minute, timezone="UTC"),
        id="daily_report",
        name="일일 크롤+Notion 리포팅",
        replace_existing=True,
    )
    log.info(
        "scheduler.start",
        hour=settings.report_hour,
        minute=settings.report_minute,
        dry_run=settings.dry_run,
    )
    scheduler.start()
