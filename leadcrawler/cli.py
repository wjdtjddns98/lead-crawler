"""명령행 진입점 (typer).

크롤러 운영·기존 import·엑셀 export·Notion 자동 리포팅을 CLI 로 노출한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import typer

from . import __version__
from .config import get_settings
from .importer import ExistingImporter
from .integrations.notion import DailyReport, NotionReporter, ScrumEntry
from .logging import configure_logging, get_logger
from .pipeline import run_pipeline
from .reporting import auto_report
from .sources.base import Segment
from .sources.segments import generate_segments
from .storage.export import ExcelExporter

app = typer.Typer(help="lead-crawler — 기업 리드 수집·검증 CLI", no_args_is_help=True)
log = get_logger("cli")


@app.command()
def version() -> None:
    """버전을 출력한다."""
    typer.echo(__version__)


@app.command()
def run(
    country: str = typer.Option("KR", help="국가 코드"),
    industry: str = typer.Option("건설", help="업종"),
    out: str = typer.Option("exports/leads.xlsx", help="엑셀 산출 경로"),
    persist: bool = typer.Option(False, help="결과를 DB 에 영속화(발견 원장 + 실존 회사)"),
) -> None:
    """단일 세그먼트를 처리하고 엑셀 서식으로 저장한다(dry_run 기본)."""
    configure_logging()
    leads = run_pipeline([Segment(country=country, industry=industry)], persist=persist)
    path = ExcelExporter().export(leads, out)
    typer.echo(f"{len(leads)}건 저장: {path}")


@app.command("run-global")
def run_global(
    industries: str = typer.Option("건설", help="쉼표구분 업종 목록(예: '건설,반도체')"),
    countries: str = typer.Option("", help="쉼표구분 국가(빈값=지원 전체국 ISO2)"),
    out: str = typer.Option("exports/leads.xlsx", help="엑셀 산출 경로"),
    persist: bool = typer.Option(False, help="결과를 DB 에 영속화(발견 원장 + 실존 회사)"),
) -> None:
    """다국가 세그먼트(국가×업종)를 일괄 처리한다(dry_run 기본).

    국가 미지정 시 지원 전체국(:mod:`countries`)을 대상으로 한다 — 한 번에 다국가 발견.
    """
    configure_logging()
    inds = [s for s in industries.split(",") if s.strip()]
    if not inds:
        raise typer.BadParameter("업종을 하나 이상 지정해야 합니다", param_hint="--industries")
    ctys = [s for s in countries.split(",") if s.strip()] or None
    segments = generate_segments(inds, countries=ctys)
    leads = run_pipeline(segments, persist=persist)
    path = ExcelExporter().export(leads, out)
    typer.echo(f"{len(segments)}개 세그먼트 → {len(leads)}건 저장: {path}")


@app.command("db-upgrade")
def db_upgrade(revision: str = typer.Argument("head", help="목표 리비전")) -> None:
    """Alembic 마이그레이션을 적용한다(기본: head)."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    configure_logging()
    # 설치형 CLI 가 어느 CWD 에서 실행돼도 동작하도록 패키지 기준 절대경로로 해석.
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(cfg, revision)
    typer.echo(f"DB 마이그레이션 적용 완료: {revision}")


@app.command("import-existing")
def import_existing(path: str = typer.Argument(..., help="기존 엑셀/CSV 경로")) -> None:
    """기존 검색분을 읽어 dedup 시드용 canonical_key 개수를 보고한다."""
    rows = ExistingImporter().read(path)
    typer.echo(f"{len(rows)}건 import, 고유 key {len({r.canonical_key for r in rows})}개")


@app.command()
def report(
    date: str = typer.Argument(..., help="보고 일자 YYYY-MM-DD"),
    done: str = typer.Option("", help="오늘 한 일"),
    nxt: str = typer.Option("", "--next", help="내일 할 일"),
    milestone: str = typer.Option("M0", help="마일스톤"),
) -> None:
    """일일 보고서 + 데일리 스크럼을 Notion 에 자동 기입한다."""
    configure_logging()
    reporter = NotionReporter(get_settings())
    reporter.post_daily_report(
        DailyReport(date=date, milestone=milestone, done=done, next=nxt)
    )
    reporter.post_scrum(ScrumEntry(date=date, today=nxt or done))
    mode = "전송" if reporter.enabled else "dry_run(미전송)"
    typer.echo(f"Notion 리포트 {mode} 완료: {date}")


@app.command("report-auto")
def report_auto(
    industries: str = typer.Option("건설", help="쉼표구분 업종 목록(예: '건설,반도체')"),
    countries: str = typer.Option("", help="쉼표구분 국가(빈값=지원 전체국 ISO2)"),
    date: str = typer.Option("", help="보고 일자 YYYY-MM-DD(빈값=오늘 UTC)"),
    milestone: str = typer.Option("M0", help="마일스톤"),
    next_plan: str = typer.Option("", "--next", help="내일 할 일(선택, 보통 비움)"),
    persist: bool = typer.Option(False, help="결과를 DB 에 영속화"),
) -> None:
    """크롤을 1회전 돌려 통계+git 활동을 모아 Notion 에 자동 기입한다(수기 입력 0).

    스케줄러가 매일 호출할 무인 리포팅 진입점. ``--done``/``--next`` 수기 입력 없이
    파이프라인 산출과 커밋 로그에서 일일보고·스크럼·현황 본문을 자동 생성한다.
    """
    configure_logging()
    report_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    inds = [s for s in industries.split(",") if s.strip()]
    if not inds:
        raise typer.BadParameter("업종을 하나 이상 지정해야 합니다", param_hint="--industries")
    ctys = [s for s in countries.split(",") if s.strip()] or None
    segments = generate_segments(inds, countries=ctys)
    leads = run_pipeline(segments, persist=persist)
    auto_report(leads, date=report_date, milestone=milestone, next_plan=next_plan)
    sent = "전송" if NotionReporter(get_settings()).enabled else "dry_run(미전송)"
    typer.echo(f"자동 리포트 {sent} 완료: {report_date} (리드 {len(leads)}건)")


@app.command("report-daily")
def report_daily(date: str = typer.Option("", help="보고 일자 YYYY-MM-DD(빈값=오늘 UTC)")) -> None:
    """설정(.env/config) 기반 무인 1회전 리포팅 — 인자 없이 동작(스케줄러용).

    업종·국가·마일스톤을 ``report_*`` 설정에서 읽으므로 OS 예약작업이 한글 인자 없이
    호출할 수 있다(Windows PowerShell 의 .ps1 한글 인코딩 함정 회피).
    """
    from .scheduler import run_daily_report

    configure_logging()
    run_daily_report(get_settings(), date=date or None)
    sent = "전송" if NotionReporter(get_settings()).enabled else "dry_run(미전송)"
    typer.echo(f"일일 리포트 {sent} 완료")


@app.command("user-add")
def user_add(
    username: str = typer.Argument(..., help="직원 로그인 아이디"),
    password: str = typer.Option(
        ..., prompt=True, hide_input=True, confirmation_prompt=True, help="비밀번호(숨김 입력)"
    ),
) -> None:
    """검증 웹앱 직원 계정을 생성한다(비밀번호는 scrypt 해시 저장)."""
    from sqlalchemy.exc import IntegrityError

    from .security import create_user
    from .storage.db import session_scope

    configure_logging()
    try:
        with session_scope(get_settings()) as s:
            create_user(s, username, password)
    except IntegrityError as exc:
        raise typer.BadParameter(f"이미 존재하는 아이디입니다: {username}") from exc
    typer.echo(f"계정 생성 완료: {username}")


@app.command("user-list")
def user_list() -> None:
    """등록된 직원 계정을 출력한다."""
    from sqlalchemy import select

    from .schema import UserRow
    from .storage.db import session_scope

    configure_logging()
    with session_scope(get_settings()) as s:
        rows = s.scalars(select(UserRow).order_by(UserRow.username)).all()
        for u in rows:
            state = "활성" if u.is_active else "비활성"
            typer.echo(f"  - {u.username} ({state})")
        typer.echo(f"총 {len(rows)}명")


@app.command("cost-report")
def cost_report(
    month: str = typer.Option("", help="집계 월 YYYY-MM(빈값=이번 달 UTC)"),
) -> None:
    """이번 달 유료 호출 과금 누계를 예산과 대비해 출력한다(cost_ledger).

    DB 에 적재된 과금(EmailAPI·Vision·딜리버러빌리티)을 월·제공자별로 집계해
    월 예산(monthly_budget_krw) 대비 사용률과 남은 예산을 보고한다.
    """
    from .cost_ledger import CostLedger, month_key_of

    configure_logging()
    settings = get_settings()
    ledger = CostLedger(settings, persist=True)
    key = month or month_key_of(datetime.now(timezone.utc))
    try:
        total = ledger.month_total_krw(key)
        breakdown = ledger.breakdown(key)
        over = ledger.is_over_budget(key)
    except Exception as exc:  # DB 미연결·테이블 없음 → 친절 안내(스택트레이스 노출 회피).
        raise typer.BadParameter(
            f"cost_ledger 조회 실패({exc}). DB 연결·마이그레이션(`db-upgrade`)을 확인하세요."
        ) from exc
    budget = settings.monthly_budget_krw
    remaining = max(0, budget - total)
    pct = (total / budget * 100) if budget else 0.0
    typer.echo(f"[{key}] 과금 누계 {total:,}원 / 예산 {budget:,}원 ({pct:.1f}%) — 남음 {remaining:,}원")
    for provider, cost in breakdown.items():
        typer.echo(f"  - {provider}: {cost:,}원")
    if over:
        typer.echo("⚠ 예산 초과 — 유료 escalation 이 차단됩니다(cost_budget_enforce).")


@app.command()
def enqueue() -> None:
    """기존 적재된 리드 중 이메일 보유 회사를 검증 큐에 백필한다(멱등).

    파이프라인은 이제 적재 시 자동 enqueue 하지만, 이미 저장된 과거 리드는 이 명령으로
    한 번 큐에 올린다. 이미 큐에 있으면 후보만 갱신(상태 보존)된다.
    """
    from sqlalchemy import select

    from .schema import ContactRow
    from .storage.db import get_sessionmaker
    from .storage.review import enqueue_email_review

    configure_logging()
    session = get_sessionmaker(get_settings())()
    try:
        rows = session.execute(
            select(ContactRow.company_id, ContactRow.value)
            .where(ContactRow.type == "email")
            .order_by(ContactRow.company_id, ContactRow.id)
        ).all()
        # 회사별로 이메일 후보를 모아 한 번에 enqueue(멀티이메일 시 후보 유실 방지).
        by_company: dict[str, list[str]] = {}
        for company_id, value in rows:
            by_company.setdefault(company_id, []).append(value)
        for company_id, values in by_company.items():
            enqueue_email_review(session, company_id, values)
        session.commit()
        typer.echo(f"검증 큐 백필 완료: 회사 {len(by_company)}곳 ({len(rows)}개 이메일) enqueue")
    finally:
        session.close()


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="바인드 호스트"),
    port: int = typer.Option(8000, help="포트"),
) -> None:
    """검증 웹앱(FastAPI)을 띄운다. fastapi/uvicorn extra(`.[api]`) 필요."""
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise typer.BadParameter(
            "uvicorn 이 없습니다. `pip install -e .[api]` 로 설치하세요."
        ) from exc

    configure_logging()
    uvicorn.run("leadcrawler.api.app:create_app", factory=True, host=host, port=port)


@app.command()
def serve() -> None:
    """24/7 스케줄러를 띄워 매일 지정 시각(UTC)에 자동 리포팅을 무인 실행한다.

    ``LEADCRAWLER_SCHEDULER_ENABLED=true`` 필요. APScheduler 미설치 시 설치 안내 후 종료.
    실행 시각·업종·국가는 ``report_*`` 설정으로 제어한다(블로킹 — Ctrl+C 로 종료).
    """
    from .scheduler import start_scheduler

    configure_logging()
    try:
        start_scheduler(get_settings())
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except KeyboardInterrupt:
        typer.echo("스케줄러 종료")


if __name__ == "__main__":
    app()
