"""명령행 진입점 (typer).

크롤러 운영·기존 import·엑셀 export·Notion 자동 리포팅을 CLI 로 노출한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import typer

from . import __version__
from .config import get_settings
from .importer import ExistingImporter, ImportedCompany
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
def import_existing(
    path: str = typer.Argument(..., help="기존 엑셀/CSV 경로(파일 또는 디렉터리)"),
    persist: bool = typer.Option(
        False, "--persist", help="discovered_company 에 dedup 시드로 저장(제약 ① 선행)"
    ),
) -> None:
    """기존 검색분을 읽어 dedup 시드(canonical_key)로 집계하고, --persist 면 DB에 적재한다.

    디렉터리를 주면 그 안의 .xlsx/.xlsm/.csv 를 모두 읽어 파일·시트를 가로질러
    canonical_key 로 중복 제거한 뒤 한 번에 처리한다.
    """
    from pathlib import Path

    p = Path(path)
    if p.is_dir():
        files = sorted(
            f for f in p.iterdir()
            if f.suffix.lower() in {".xlsx", ".xlsm", ".csv"} and not f.name.startswith("~$")
        )
    else:
        files = [p]
    if not files:
        typer.echo(f"대상 파일이 없습니다: {path}")
        raise typer.Exit(code=1)

    importer = ExistingImporter()
    uniq: dict[str, ImportedCompany] = {}  # canonical_key → 회사(파일·시트 가로질러 dedup)
    for f in files:
        rows = importer.read(f)
        for r in rows:
            uniq.setdefault(r.canonical_key, r)
        typer.echo(f"  {f.name}: {len(rows)}건")
    typer.echo(f"총 파싱: 고유 기업 {len(uniq)}개 (파일 {len(files)}개)")

    if not persist:
        typer.echo("(--persist 미지정 — DB 저장 안 함)")
        return

    from .storage.db import get_sessionmaker
    from .storage.repository import seed_discovered_from_imports

    session = get_sessionmaker(get_settings())()
    try:
        new, skipped = seed_discovered_from_imports(session, uniq.values())
        session.commit()
    finally:
        session.close()
    typer.echo(f"DB 시드 완료: 신규 {new}건, 기존 스킵 {skipped}건 (source='import')")


@app.command("dedup-report")
def dedup_report(
    out: str = typer.Option("exports/dedup_report.json", help="리포트 JSON 산출 경로"),
    min_score: float = typer.Option(
        84.0, help="이름 유사도 쇼트리스트 하한(이상만 후보, 0~100)"
    ),
    strong_score: float = typer.Option(
        90.0, help="이름 高 임계(이상 + 도메인일치면 auto 자동제거 후보)"
    ),
    max_block: int = typer.Option(
        1000, help="블록당 비교 상한(초과 블록은 O(n²) 폭발 방지로 생략·보고)"
    ),
    include_merged: bool = typer.Option(
        False, "--include-merged", help="이미 머지된 행(duplicate_of)도 비교 대상에 포함"
    ),
    llm_judge: bool = typer.Option(
        False,
        "--llm-judge",
        help="쇼트리스트 티어를 Claude(Haiku)로 동일기업 판정(C2·유료, dry_run=스텁). "
        "설정 LEADCRAWLER_DEDUP_LLM_JUDGE=true 로도 켤 수 있음",
    ),
) -> None:
    """발견 원장(discovered_company) 전건에서 중복 후보 쌍을 찾아 JSON 리포트로 저장한다.

    수집 파이프라인과 무관한 읽기전용 오프라인 배치(dry_run 무관). 블로킹 + rapidfuzz
    토큰셋 유사도 + 도메인root 일치로 결정적 분류한다. 자동제거는 최상위(auto) 티어만
    가역적으로 제안하고, 나머지는 LLM/사람 검토 쇼트리스트로 남긴다(제약② 리드손실 방지).
    """
    # 유사도 점수는 0~100 범위이고 min<=strong 이어야 한다. strong 을 100 초과로 주면
    # auto/keep_both 가 영영 도달 불가(조용히 auto_removable=0)라, 범위까지 검증한다.
    if not 0.0 <= min_score <= strong_score <= 100.0:
        raise typer.BadParameter(
            f"임계값은 0 <= --min-score({min_score}) <= --strong-score({strong_score}) <= 100 "
            "이어야 합니다",
            param_hint="--min-score/--strong-score",
        )

    from .dedup_resolve.report import run_dedup_report
    from .storage.db import get_sessionmaker

    configure_logging()
    settings = get_settings()
    # C2(opt-in): --llm-judge 플래그 또는 설정 dedup_llm_judge 중 하나라도 켜지면 판정.
    do_judge = llm_judge or settings.dedup_llm_judge
    # 판정기·원장을 dry_run/키 유무에 맞춰 구성. dry_run·키없음=무료 스텁.
    judge = ledger = None
    if do_judge:
        from .cost_ledger import CostLedger
        from .dedup_resolve.llm_judge import build_judge

        judge = build_judge(settings)
        ledger = CostLedger(settings, persist=not settings.dry_run)

    session = get_sessionmaker(settings)()
    try:
        rpt = run_dedup_report(
            session,
            out,
            include_merged=include_merged,
            name_strong=strong_score,
            name_medium=min_score,
            max_block_size=max_block,
            judge=judge,
            ledger=ledger,
            judge_max_pairs=settings.dedup_llm_max_pairs,
        )
    finally:
        session.close()
    typer.echo(
        f"중복 리포트 저장: {out} / 레코드 {rpt.total_records:,}건 중 후보 {rpt.total_candidates:,}쌍 "
        f"(자동제거 가능 {rpt.auto_removable:,}쌍, 둘다유지 {rpt.keep_both:,}쌍)"
    )
    for tier, count in sorted(rpt.by_tier.items()):
        typer.echo(f"  - {tier}: {count:,}쌍")
    if do_judge:
        same = sum(1 for j in rpt.judged if j.verdict.same)
        mode = "스텁(dry_run/키없음)" if (settings.dry_run or not settings.anthropic_api_key) else "Claude"
        typer.echo(
            f"LLM 판정({mode}): 쇼트리스트 {rpt.llm_judged_count:,}쌍 판정 → 동일 {same:,}쌍 / 미확정 "
            f"{rpt.llm_judged_count - same:,}쌍 (확정 머지는 C3/C4 위임)"
        )
    if rpt.skipped_blocks:
        skipped_pairs = sum(b.size for b in rpt.skipped_blocks)
        typer.echo(
            f"⚠ 크기초과로 생략된 블록 {len(rpt.skipped_blocks):,}개(레코드 {skipped_pairs:,}건) "
            f"— --max-block 을 높여 완전 재실행 가능"
        )
    typer.echo("주의: C1 은 비완전(이름·도메인 둘 다 다른 동일기업은 C2/C4 위임)")


@app.command("dedup-merge")
def dedup_merge(
    report_path: str = typer.Option(
        "exports/dedup_report.json", "--report", help="dedup-report 가 만든 리포트 JSON 경로"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="실제 머지 적용(미지정=미리보기만, DB 안 건드림)"
    ),
    include_llm: bool = typer.Option(
        False, "--include-llm", help="auto 티어 외에 LLM 판정 same=True 쌍도 확정 중복으로 포함"
    ),
) -> None:
    """중복 리포트의 **확정 쌍**(auto 티어 + 선택적 LLM same)에서 골든레코드(C3)를 산정한다.

    기본은 최상위 auto 티어만 자동 머지 대상(제약② 리드손실 방지). ``--include-llm`` 으로
    Claude 가 same 으로 판정한 쇼트리스트도 포함할 수 있다. ``--apply`` 없으면 미리보기만.
    """
    import json
    from pathlib import Path

    from .dedup_resolve.golden import apply_golden, load_cluster_members, resolve_all
    from .storage.db import get_sessionmaker

    configure_logging()
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    # 확정 쌍 수집: auto 티어는 항상, LLM same 은 opt-in. (key_a<key_b 는 리포트에서 보장됨)
    pairs: list[tuple[str, str]] = [
        (c["key_a"], c["key_b"]) for c in data.get("candidates", []) if c.get("tier") == "auto"
    ]
    if include_llm:
        pairs += [
            (j["candidate"]["key_a"], j["candidate"]["key_b"])
            for j in data.get("judged", [])
            if j.get("verdict", {}).get("same")
        ]
    if not pairs:
        typer.echo("확정 중복 쌍이 없습니다(auto 티어 0건). 머지할 것 없음.")
        return

    keys = {k for pair in pairs for k in pair}
    session = get_sessionmaker(get_settings())()
    try:
        members = load_cluster_members(session, keys)
        goldens = resolve_all(members, pairs, basis="auto+llm" if include_llm else "auto")
        total_absorbed = sum(len(g.absorbed_keys) for g in goldens)
        typer.echo(
            f"확정 쌍 {len(pairs):,}개 → 클러스터 {len(goldens):,}개 / 흡수대상 {total_absorbed:,}건"
            + ("" if apply else " (미리보기 — --apply 로 실제 머지)")
        )
        for g in goldens[:20]:
            typer.echo(f"  생존 {g.survivor_key} ← {len(g.absorbed_keys)}건 / 캐노니컬명='{g.canonical_name}'")
        if len(goldens) > 20:
            typer.echo(f"  …외 {len(goldens) - 20:,}개")
        if apply:
            applied = sum(apply_golden(session, g, merged_by="auto") for g in goldens)
            session.commit()
            typer.echo(f"머지 적용 완료: {applied:,}건 흡수(duplicate_of 기록·가역). ")
    finally:
        session.close()


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
    role: str = typer.Option("worker", help="권한 admin|worker(첫 계정은 자동 admin)"),
) -> None:
    """검증 웹앱 직원 계정을 생성한다(비밀번호는 scrypt 해시 저장)."""
    from sqlalchemy.exc import IntegrityError

    from .security import create_user
    from .storage.db import session_scope

    configure_logging()
    try:
        with session_scope(get_settings()) as s:
            user = create_user(s, username, password, role=role)
            created_role = user.role  # 부트스트랩으로 admin 승격됐을 수 있어 실제값 표시.
    except ValueError as exc:  # 허용되지 않은 역할.
        raise typer.BadParameter(str(exc)) from exc
    except IntegrityError as exc:
        raise typer.BadParameter(f"이미 존재하는 아이디입니다: {username}") from exc
    typer.echo(f"계정 생성 완료: {username} ({created_role})")


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
            typer.echo(f"  - {u.username} [{u.role}] ({state})")
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
        rpt = ledger.report(key)
    except Exception as exc:  # DB 미연결·테이블 없음 → 친절 안내(스택트레이스 노출 회피).
        raise typer.BadParameter(
            f"cost_ledger 조회 실패({exc}). DB 연결·마이그레이션(`db-upgrade`)을 확인하세요."
        ) from exc
    typer.echo(
        f"[{rpt['month_key']}] 과금 누계 {rpt['total_krw']:,}원 / 예산 {rpt['budget_krw']:,}원 "
        f"({rpt['pct']}%) — 남음 {rpt['remaining_krw']:,}원"
    )
    for provider, cost in rpt["breakdown"].items():
        typer.echo(f"  - {provider}: {cost:,}원")
    if rpt["over_budget"]:
        typer.echo("⚠ 예산 초과 — 유료 escalation 이 차단됩니다(cost_budget_enforce).")


@app.command()
def enqueue() -> None:
    """기존 적재된 **실존 회사 전체**를 검증 큐에 백필한다(이메일 없어도 — 멱등).

    파이프라인은 이제 적재 시 자동 enqueue 하지만, 규칙 변경 전 저장된 과거 리드는 이
    명령으로 한 번 큐에 올린다. 이메일 후보가 있으면 후보를 싣고, 없으면 빈 후보로 등록해
    사람이 워크벤치에서 직접 이메일을 찾거나 문의폼으로 처리한다. 이미 큐에 있으면 후보만
    갱신(상태·선택 보존)된다.
    """
    from sqlalchemy import select

    from .schema import CompanyRow, ContactRow
    from .storage.db import get_sessionmaker
    from .storage.review import enqueue_email_review

    configure_logging()
    session = get_sessionmaker(get_settings())()
    try:
        # 회사별 이메일 후보 맵(있는 회사만).
        emails = session.execute(
            select(ContactRow.company_id, ContactRow.value)
            .where(ContactRow.type == "email")
            .order_by(ContactRow.company_id, ContactRow.id)
        ).all()
        by_company: dict[str, list[str]] = {}
        for company_id, value in emails:
            by_company.setdefault(company_id, []).append(value)
        # 실존(active) 회사 전체를 enqueue — 이메일 없는 회사도 빈 후보로 포함.
        active_ids = list(
            session.scalars(select(CompanyRow.id).where(CompanyRow.is_active.is_(True))).all()
        )
        for company_id in active_ids:
            enqueue_email_review(session, company_id, by_company.get(company_id, []))
        session.commit()
        with_email = sum(1 for cid in active_ids if cid in by_company)
        typer.echo(
            f"검증 큐 백필 완료: 실존 회사 {len(active_ids)}곳 enqueue "
            f"(이메일 보유 {with_email}곳 / 이메일 없음 {len(active_ids) - with_email}곳)"
        )
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
