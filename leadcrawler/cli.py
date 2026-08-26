"""명령행 진입점 (typer).

크롤러 운영·기존 import·엑셀 export·Notion 자동 리포팅을 CLI 로 노출한다.
"""

from __future__ import annotations

import time
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
from .sources.segments import generate_segments, parse_regions
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
    regions: str = typer.Option(
        "", help="KR 지역별 검색 팬아웃 — 'all'(17개 시/도) 또는 쉼표구분('서울,경기'). KR 전용"
    ),
    out: str = typer.Option("exports/leads.xlsx", help="엑셀 산출 경로"),
    persist: bool = typer.Option(False, help="결과를 DB 에 영속화(발견 원장 + 실존 회사)"),
) -> None:
    """다국가 세그먼트(국가×업종)를 일괄 처리한다(dry_run 기본).

    국가 미지정 시 지원 전체국(:mod:`countries`)을 대상으로 한다 — 한 번에 다국가 발견.
    ``--regions`` 는 KR 세그먼트를 지역별 검색 세그먼트로 팬아웃한다(다른 국가는 무시).
    """
    configure_logging()
    inds = [s for s in industries.split(",") if s.strip()]
    if not inds:
        raise typer.BadParameter("업종을 하나 이상 지정해야 합니다", param_hint="--industries")
    ctys = [s for s in countries.split(",") if s.strip()] or None
    segments = generate_segments(inds, countries=ctys, regions=parse_regions(regions))
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


class _ManagedJob:
    """``--job-id`` 관리형 실행 컨텍스트 — 진행 자기보고·취소 폴링(#352 PR③).

    웹 supervisor 가 만든 backfill_job 행에 배치마다 카운터를 자기보고하고, 취소
    플래그를 짧은 세션으로 폴링한다(장수 세션 identity-map 캐시는 다른 트랜잭션의
    취소 커밋을 못 본다 — crawl_job 소비자와 동일 회피). ``job_id=None``(비관리형
    — bat 러너·수동 실행)이면 전부 no-op 으로 수렴해 기존 동작과 100% 동일하다.
    """

    def __init__(self, sm, job_id: str | None, generation: int) -> None:  # noqa: ANN001
        self._sm = sm
        self._job_id = job_id
        self._generation = generation

    def cancelled(self) -> bool:
        if self._job_id is None:
            return False
        from .storage.backfill_job import is_cancel_requested

        with self._sm() as s:
            return is_cancel_requested(s, self._job_id)

    def should_stop(self) -> bool:
        """중지 신호 — 취소뿐 아니라 **세대 교체·작업 종료·행 삭제**도 감지한다.

        idle 대기 중인 구세대 자식은 배치 보고(펜싱)를 안 하므로, 이 확인이 없으면
        트랙 잠금을 무기한 쥔 채 새 세대의 기동을 막는다(2026-08-18 Codex HIGH-3).
        """
        if self._job_id is None:
            return False
        from .storage.backfill_job import TERMINAL, get_backfill_job

        with self._sm() as s:
            row = get_backfill_job(s, self._job_id)
            if row is None:
                return True
            return bool(
                row.cancel_requested
                or row.status in TERMINAL
                or row.generation != self._generation
            )

    def invalid_reason(self, track: str) -> str | None:
        """시작 전 job 검증 — 없음/트랙 불일치/이미 종료면 사유 문자열(fail-loud 용)."""
        if self._job_id is None:
            return None
        from .storage.backfill_job import TERMINAL, get_backfill_job

        with self._sm() as s:
            row = get_backfill_job(s, self._job_id)
            if row is None:
                return f"job {self._job_id} 없음"
            if row.track != track:
                return f"job 트랙 불일치({row.track} != {track})"
            if row.status in TERMINAL:
                return f"job 이미 종료({row.status})"
        return None

    def report(self, *, remaining: int | None = None, **deltas: int) -> bool:
        """배치 자기보고 — DB 가 거부하면(세대 교체·종료) False: 자식은 스스로 멈춰야
        한다(유령 세대가 과금 작업을 계속하는 유일한 감지선, 2026-08-18 Codex HIGH-2)."""
        if self._job_id is None:
            return True
        from .storage.backfill_job import record_progress

        with self._sm() as s:
            ok = record_progress(
                s, self._job_id, self._generation, remaining=remaining, **deltas
            )
            s.commit()
        return ok

    def wait(self, seconds: float) -> bool:
        """대기 — 관리형이면 5초 청크로 취소를 살피고, 취소 시 True(조기 반환)."""

        if self._job_id is None:
            time.sleep(seconds)
            return False
        end = time.monotonic() + seconds
        while True:
            if self.should_stop():
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(5.0, left))


def _open_run(settings):  # noqa: ANN001, ANN202
    """관리형 CLI 런당 1회 여는 공유 컴포넌트(:class:`PromoteRun`) — 테스트가 스텁하는 지점.

    ponytail: 관리형 자식은 루프 종료 = 프로세스 종료라 registry 체커를 명시 close 하지 않는다
    (OS 가 회수). 장수 프로세스에서 쓰게 되면 try/finally 로 ``run.close()`` 를 넣을 것.
    """
    from .pipeline.fill import PromoteRun

    return PromoteRun.open(settings)


def _throttled(should_stop, *, sec: float = 5.0):  # noqa: ANN001, ANN202
    """짧은 세션을 여는 폴 함수(예: ``_ManagedJob.should_stop``)를 스로틀한다.

    ``background._make_cancel_poller`` 와 동일 관례 — 기업/세그먼트 단위로 매번 DB 를
    왕복하면 그 자체가 병목이 된다. ``sec`` 마다 1회만 조회하고 사이엔 마지막 값을 반환.
    첫 호출은 즉시 조회(시작 전 취소·pause 즉시 반영). 한 번 True 면 계속 True(래치).
    """
    last = float("-inf")
    latched = False

    def _poll() -> bool:
        nonlocal last, latched
        if latched:
            return True
        now = time.monotonic()
        if now - last < sec:
            return False
        last = now
        if should_stop():
            latched = True
            return True
        return False

    return _poll


def _acquire_track_lock_or_exit(settings, track: str, label: str):  # noqa: ANN001, ANN202
    """트랙 실행 잠금 획득 — 이미 점유면 안내 후 종료(중복 실행이 과금 이중화를 만듦).

    반환 핸들은 프로세스 수명 동안 보유한다(커넥션 종료 = 잠금 해제). SQLite 는 no-op.
    """
    from .storage.db import get_engine
    from .storage.track_lock import acquire_track_lock

    lock = acquire_track_lock(get_engine(settings), track)
    if lock is None:
        typer.echo(f"[{label}] 트랙 {track} 실행 잠금 점유 중(다른 백필 실행) — 중단.")
        raise typer.Exit(1)
    return lock


@app.command("fill-emails")
def fill_emails(
    loop: bool = typer.Option(False, "--loop", help="상시 consumer — 취소 전까지 배치 반복"),
    batch: int = typer.Option(200, help="배치당 최대 회사 수(쿼리 LIMIT)"),
    workers: int = typer.Option(6, help="enrich 병렬(헤드리스 경합 방지 위해 발견보다 낮게)"),
    interval: float = typer.Option(30.0, help="--loop: 대상 부족/소진 시 폴링 대기(초)"),
    min_queue: int = typer.Option(20, help="--loop: 대상이 이 수 이상 쌓이면 배치 처리(그 전엔 대기)"),
    max_batches: int = typer.Option(
        0, min=0, help="--loop: 이 배치 수 처리 후 정상종료(0=무제한) — 러너 재기동용 메모리 리셋"
    ),
    country: list[str] = typer.Option(
        None, "--country", help="이 국가만 대상(반복 지정 가능, 예: --country KR) — 미지정=전세계"
    ),
    industry: list[str] = typer.Option(
        None, "--industry",
        help="이 업종만 대상(정규 라벨, 반복 지정·쉼표 병기 가능 — 굶는 세그먼트 타겟 보충."
        " '미분류'=라벨 빈값 행, /queue/stock 뱃지 값과 동일 어휘)",
    ),
    exclude_industry: list[str] = typer.Option(
        None, "--exclude-industry",
        help="이 업종 제외(정규 라벨, 반복 지정·쉼표 병기 가능: --exclude-industry '은행,보험')",
    ),
    exclude_listed: bool = typer.Option(
        False, "--exclude-listed", help="상장 확정(listed='listed') 제외 — unknown 은 대상 유지"
    ),
    job_id: str = typer.Option(
        None, "--job-id", help="관리형(웹 supervisor) 실행 — backfill_job 행에 진행 자기보고"
    ),
    job_generation: int = typer.Option(
        0, "--job-generation", help="관리형: 이 프로세스의 세대(진행 보고 펜싱 키)"
    ),
    stall_exit_secs: float = typer.Option(
        900.0, "--stall-exit-secs", min=0,
        help="배치 진행이 이 초 동안 정체되면 프로세스 종료(러너/supervisor 재기동용, 0=끔)"
        " — Playwright 드라이버 무응답 행 복구(2026-08-18 실사고)",
    ),
) -> None:
    """큐의 '실존·무이메일' 회사에 이메일을 배치 병렬로 채운다(발견 producer 의 consumer).

    발견 크롤(discovery_only)이 회사+홈페이지를 빠르게 큐에 쌓으면, 이 소비자가 무이메일
    회사를 배치로 잡아 헤드리스/OCR 까지 돌려 이메일을 채운다. 멱등(채워지면 대상에서 이탈).
    ``--loop`` 면 취소(Ctrl-C) 전까지 상시 구동한다.
    """
    from .pipeline.fill import count_targets, fill_batch
    from .storage.db import get_sessionmaker

    configure_logging()
    settings = get_settings()
    if settings.dry_run:
        typer.echo("DRY_RUN=true — 채우기 무의미(더미 반환). 중단.")
        raise typer.Exit(1)
    sm = get_sessionmaker(settings)
    # 직접 호출(테스트) 시 기본값이 OptionInfo 객체로 들어온다 — list 일 때만 스코프로 인정.
    scope = list(country) if isinstance(country, list) and country else None
    # 쉼표 병기 허용 — bat 러너의 인자 슬롯 한계로 11개 업종을 한 토큰에 담는 경로.
    incl_ind = (
        [t.strip() for v in industry for t in v.split(",") if t.strip()]
        if isinstance(industry, list) and industry else None
    )
    excl_ind = (
        [t.strip() for v in exclude_industry for t in v.split(",") if t.strip()]
        if isinstance(exclude_industry, list) and exclude_industry else None
    )
    excl_listed = exclude_listed is True  # OptionInfo 방어(위와 동일).
    filters = {
        "industries": incl_ind, "exclude_industries": excl_ind, "exclude_listed": excl_listed,
    }
    # 정체 종료는 CLI 전용(프로세스를 러너/supervisor 가 재기동) — OptionInfo 방어 동일.
    # filters 와 분리 유지: filters 는 count_targets 에도 풀리는데 거긴 이 인자가 없다.
    stall = stall_exit_secs if isinstance(stall_exit_secs, (int, float)) else 900.0
    stall_s = stall if stall > 0 else None
    mj = _ManagedJob(
        sm,
        job_id if isinstance(job_id, str) and job_id else None,
        job_generation if isinstance(job_generation, int) else 0,
    )
    # 트랙 실행 잠금 — 웹·bat·야간 CLI 중복 실행 차단(과금 이중화 방지). 명시 해제 없이
    # 프로세스 수명 동안 보유한다(1 CLI 호출 = 1 프로세스 불변식 — 같은 프로세스에서
    # 이 커맨드를 재호출하면 PG 에선 자기 잠금에 막힌다. 재호출 용도가 생기면 try/finally).
    _lock = _acquire_track_lock_or_exit(settings, "A", "fill")
    _run = _open_run(settings)  # 런당 1회 — LLM 호출 상한·registry 체커를 배치 간 공유.
    bad = mj.invalid_reason("A")
    if bad:
        typer.echo(f"[fill] 관리형 job 검증 실패 — {bad}. 중단.")
        raise typer.Exit(1)

    if not loop:
        if mj.cancelled():
            typer.echo("[fill] 취소 요청 감지 — 실행 없이 종료.")
            return
        processed, emails = fill_batch(
            settings, sm, limit=batch, workers=workers, countries=scope,
            stall_exit_s=stall_s, **filters,
            run=_run,
        )
        mj.report(processed=processed, emails=emails, batches=1)
        typer.echo(f"[fill] 처리 {processed}, 신규이메일 {emails}")
        return

    typer.echo(
        f"[fill] 상시 consumer 시작 (batch={batch} workers={workers} min_queue={min_queue}"
        f" country={scope or '전세계'} industry={incl_ind or '전체'}"
        f" exclude_industry={excl_ind or '없음'} exclude_listed={excl_listed})"
    )
    batches = 0
    while True:  # 취소 = Ctrl-C / 프로세스 종료 / 관리형 취소 플래그.
        if mj.should_stop():
            typer.echo("[fill] 중지 신호 감지(취소/세대 교체/종료) — 정상종료.")
            return
        pending = count_targets(sm, scope, **filters)
        if pending < min_queue:  # 임계 미만 → 더 쌓일 때까지 대기(배치 효율).
            if mj.wait(interval):
                typer.echo("[fill] 중지 신호 감지(취소/세대 교체/종료) — 정상종료.")
                return
            continue
        processed, emails = fill_batch(
            settings, sm, limit=batch, workers=workers, countries=scope,
            stall_exit_s=stall_s, **filters,
            run=_run,
        )
        # ponytail: remaining 은 배치 직전 카운트(한 배치 지연 근사) — 정밀해지면 재count.
        if not mj.report(processed=processed, emails=emails, batches=1, remaining=pending):
            typer.echo("[fill] 진행 보고 거부(세대 교체/작업 종료) — 정상종료.")
            return
        typer.echo(f"[fill] 배치 처리 {processed}, 신규이메일 {emails}, 대기 {pending}")
        batches += 1
        # 장기구동 시 메모리 누적 대비 — 러너(bat)가 재기동해 리셋한다(멱등이라 이어받음).
        if max_batches and batches >= max_batches:
            typer.echo(f"[fill] max_batches={max_batches} 도달 — 정상종료(러너 재기동 대상)")
            return
        if processed == 0:  # 대상 있었으나 다 실패/이탈 → 폭주 방지 대기.
            if mj.wait(interval):
                typer.echo("[fill] 중지 신호 감지(취소/세대 교체/종료) — 정상종료.")
                return


@app.command("backfill-resolve-domains")
def backfill_resolve_domains(
    loop: bool = typer.Option(False, "--loop", help="상시 consumer — 취소 전까지 배치 반복"),
    batch: int = typer.Option(200, help="배치당 최대 회사 수(쿼리 LIMIT)"),
    workers: int = typer.Option(4, help="해석+enrich 병렬(검색 API 레이트 고려해 보수적)"),
    interval: float = typer.Option(60.0, help="--loop: 대상 부족/소진 시 폴링 대기(초)"),
    min_queue: int = typer.Option(20, help="--loop: 대상이 이 수 이상 쌓이면 배치 처리(그 전엔 대기)"),
    max_batches: int = typer.Option(
        0, min=0, help="--loop: 이 배치 수 처리 후 정상종료(0=무제한) — 러너 재기동용 메모리 리셋"
    ),
    country: list[str] = typer.Option(
        None, "--country", help="이 국가만 대상(반복 지정 가능, 예: --country KR) — 미지정=전세계"
    ),
    industry: list[str] = typer.Option(
        None, "--industry",
        help="이 업종만 대상(반복 지정·쉼표 병기 가능 — 굶는 세그먼트 타겟 보충."
        " 미승격 행은 발견 라벨 기준, '미분류'=라벨 빈값 행)",
    ),
    exclude_industry: list[str] = typer.Option(
        None, "--exclude-industry",
        help="이 업종 제외(반복 지정·쉼표 병기 가능 — 미승격 행은 발견 라벨 기준)",
    ),
    exclude_listed: bool = typer.Option(
        False, "--exclude-listed", help="상장 확정(listed='listed') 제외 — unknown 은 대상 유지"
    ),
    job_id: str = typer.Option(
        None, "--job-id", help="관리형(웹 supervisor) 실행 — backfill_job 행에 진행 자기보고"
    ),
    job_generation: int = typer.Option(
        0, "--job-generation", help="관리형: 이 프로세스의 세대(진행 보고 펜싱 키)"
    ),
    stall_exit_secs: float = typer.Option(
        900.0, "--stall-exit-secs", min=0,
        help="배치 진행이 이 초 동안 정체되면 프로세스 종료(러너/supervisor 재기동용, 0=끔)"
        " — Playwright 드라이버 무응답 행 복구(2026-08-18 실사고)",
    ),
) -> None:
    """도메인 없이 발견돼 정체된 회사(전세계, GLEIF·NPS 등)에 도메인 해석부터 다시
    돌려 승격을 시도한다.

    최초 발견 때 도메인을 못 준 소스는 dedup 원장에 이름 키로만 남고, 발견 파이프라인의
    dedup(제약①) 때문에 재크롤로도 다시 잡히지 않는다 — 이 사각을 되짚는 전용 소비자.
    KR 은 네이버(무료 25k/일), 그 외는 유료 CSE/Serper 로 라우팅된다 — 쿼터·예산 소진
    중엔 처리수만 늘고 해석은 거의 안 될 수 있다(정상 — 리셋 후 재실행하면 이어서 풀린다).
    """
    from .pipeline.fill import count_resolve_targets, resolve_batch
    from .storage.db import get_sessionmaker

    configure_logging()
    settings = get_settings()
    if settings.dry_run:
        typer.echo("DRY_RUN=true — 해석 무의미(더미 반환). 중단.")
        raise typer.Exit(1)
    if not settings.resolve_domains:
        typer.echo("LEADCRAWLER_RESOLVE_DOMAINS=false — 해석 비활성. 중단.")
        raise typer.Exit(1)
    sm = get_sessionmaker(settings)
    # 직접 호출(테스트) 시 기본값이 OptionInfo 객체로 들어온다 — list 일 때만 스코프로 인정.
    scope = list(country) if isinstance(country, list) and country else None
    # 쉼표 병기 허용 — bat 러너 인자 슬롯 한계 대응(fill-emails 와 동일 관례).
    excl_ind = (
        [t.strip() for v in exclude_industry for t in v.split(",") if t.strip()]
        if isinstance(exclude_industry, list) and exclude_industry else None
    )
    incl_ind = (
        [t.strip() for v in industry for t in v.split(",") if t.strip()]
        if isinstance(industry, list) and industry else None
    )
    excl_listed = exclude_listed is True  # OptionInfo 방어(위와 동일).
    filters = {
        "industries": incl_ind, "exclude_industries": excl_ind, "exclude_listed": excl_listed,
    }
    # 정체 종료는 CLI 전용 — filters 와 분리(count_resolve_targets 엔 이 인자가 없다).
    stall = stall_exit_secs if isinstance(stall_exit_secs, (int, float)) else 900.0
    stall_s = stall if stall > 0 else None
    mj = _ManagedJob(
        sm,
        job_id if isinstance(job_id, str) and job_id else None,
        job_generation if isinstance(job_generation, int) else 0,
    )
    # 트랙 실행 잠금 — 웹·bat·야간 CLI 중복 실행 차단(과금 이중화 방지). 명시 해제 없이
    # 프로세스 수명 동안 보유한다(1 CLI 호출 = 1 프로세스 불변식 — 같은 프로세스에서
    # 이 커맨드를 재호출하면 PG 에선 자기 잠금에 막힌다. 재호출 용도가 생기면 try/finally).
    _lock = _acquire_track_lock_or_exit(settings, "C", "resolve")
    _run = _open_run(settings)  # 런당 1회 — LLM 호출 상한·registry 체커를 배치 간 공유.
    bad = mj.invalid_reason("C")
    if bad:
        typer.echo(f"[resolve] 관리형 job 검증 실패 — {bad}. 중단.")
        raise typer.Exit(1)

    if not loop:
        if mj.cancelled():
            typer.echo("[resolve] 취소 요청 감지 — 실행 없이 종료.")
            return
        processed, resolved, promoted = resolve_batch(
            settings, sm, limit=batch, workers=workers, countries=scope,
            stall_exit_s=stall_s, **filters,
            run=_run,
        )
        mj.report(processed=processed, resolved=resolved, promoted=promoted, batches=1)
        typer.echo(f"[resolve] 처리 {processed}, 도메인해석 {resolved}, 신규승격 {promoted}")
        return

    typer.echo(
        f"[resolve] 상시 consumer 시작 (batch={batch} workers={workers} min_queue={min_queue}"
        f" country={scope or '전세계'} industry={incl_ind or '전체'}"
        f" exclude_industry={excl_ind or '없음'} exclude_listed={excl_listed})"
    )
    batches = 0
    while True:  # 취소 = Ctrl-C / 프로세스 종료 / 관리형 취소 플래그.
        if mj.should_stop():
            typer.echo("[resolve] 중지 신호 감지(취소/세대 교체/종료) — 정상종료.")
            return
        pending = count_resolve_targets(sm, scope, **filters)
        if pending < min_queue:
            if mj.wait(interval):
                typer.echo("[resolve] 중지 신호 감지(취소/세대 교체/종료) — 정상종료.")
                return
            continue
        processed, resolved, promoted = resolve_batch(
            settings, sm, limit=batch, workers=workers, countries=scope,
            stall_exit_s=stall_s, **filters,
            run=_run,
        )
        # ponytail: remaining 은 배치 직전 카운트(한 배치 지연 근사) — 정밀해지면 재count.
        if not mj.report(
            processed=processed, resolved=resolved, promoted=promoted,
            batches=1, remaining=pending,
        ):
            typer.echo("[resolve] 진행 보고 거부(세대 교체/작업 종료) — 정상종료.")
            return
        typer.echo(f"[resolve] 배치 처리 {processed}, 해석 {resolved}, 승격 {promoted}, 대기 {pending}")
        batches += 1
        # 장기구동 시 메모리 누적 대비 — 러너(bat)가 재기동해 리셋한다(멱등이라 이어받음).
        if max_batches and batches >= max_batches:
            typer.echo(f"[resolve] max_batches={max_batches} 도달 — 정상종료(러너 재기동 대상)")
            return
        if processed == 0 or resolved == 0:  # 소진/무진전 → 폭주 방지 대기.
            if mj.wait(interval):
                typer.echo("[resolve] 중지 신호 감지(취소/세대 교체/종료) — 정상종료.")
                return


@app.command("segment-run")
def segment_run(
    job_id: str = typer.Option(..., "--job-id", help="트랙 S 작업 행 id(필수 — 관리형 전용)"),
    job_generation: int = typer.Option(
        ..., "--job-generation", help="이 프로세스의 세대(진행 보고 펜싱 키, 필수)"
    ),
    batch: int = typer.Option(100, help="promote 배치당 최대 회사 수"),
    workers: int = typer.Option(3, help="promote 단계 병렬"),
    max_batches: int = typer.Option(
        20, min=1, help="promote 배치 이 수만큼 처리 후 정상종료(세대교체용 메모리 리셋)"
    ),
    stall_exit_secs: float = typer.Option(
        900.0, "--stall-exit-secs", min=0,
        help="발견/승격 진행이 이 초 동안 정체되면 프로세스 종료(재기동용, 0=끔)",
    ),
) -> None:
    """트랙 S(세그먼트 승격 큐) 자식 — 발견(record_only)→승격(promote) 을 순차 처리한다.

    필터(countries/industries/listed/regions)는 CLI 인자가 아니라 ``backfill_job`` 행에서
    로드한다(웹 요청 그대로 재현 — supervisor 가 만든 요청과 자식 인자가 어긋나지 않게).
    **DRY_RUN 게이트는 적용하지 않는다**(다른 관리형 CLI 와 차이) — ``run_pipeline``·
    ``_build_lead``·``promote_batch`` 는 dry_run 에서도 네트워크 없이 결정적 더미로
    동작해, 테스트가 review_queue 적재까지 실제로 검증할 수 있다(설계 §3).
    """
    from .pipeline.promote import (
        PromoteRun,
        _load_domain_guards,
        count_promote_targets,
        promote_batch,
    )
    from .storage.backfill_job import RUNNING, TRACK_S, get_backfill_job
    from .storage.db import get_sessionmaker

    configure_logging()
    settings = get_settings()
    sm = get_sessionmaker(settings)
    mj = _ManagedJob(sm, job_id, job_generation)
    # 트랙 실행 잠금 — 다른 S 자식·수동 스크립트 중복 실행 차단(과금 이중화 방지).
    _lock = _acquire_track_lock_or_exit(settings, "S", "segment-run")

    with sm() as s:
        row = get_backfill_job(s, job_id)
    if (
        row is None
        or row.track != TRACK_S
        or row.status != RUNNING
        or row.generation != job_generation
    ):
        state = "없음" if row is None else f"{row.track}/{row.status}/gen={row.generation}"
        typer.echo(f"[segment-run] job 검증 실패({state}) — 중단.")
        raise typer.Exit(1)

    ctys = [c.strip() for c in row.countries.split(",") if c.strip()] or None
    inds = [i.strip() for i in row.industries.split(",") if i.strip()]
    if not ctys or not inds:
        # 빈 필터는 _scoped 가 무필터로 흡수 → 전 우주 승격(리뷰 MED). enqueue 가드의 2중 방어.
        typer.echo("[segment-run] countries/industries 가 비어 있음 — 중단.")
        raise typer.Exit(1)
    regs = parse_regions(row.regions)
    listed = row.listed
    stall_s = stall_exit_secs if stall_exit_secs > 0 else None
    should_stop = _throttled(mj.should_stop)

    def _rejected() -> None:
        typer.echo("[segment-run] 진행 보고 거부(세대 교체/일시정지) — 종료.")

    if row.stage in ("", "discover"):
        # 유료 발견 전에 세대 펜싱을 먼저 확인하고 stage='discover' 를 기록(''=미시작과 구분).
        if not mj.report(stage="discover"):
            _rejected()
            return
        segments = generate_segments(inds, countries=ctys, listed=[listed], regions=regs)
        seg_discovered = 0
        reported = 0
        rejected = False
        last_report_t = time.monotonic()

        def _on_progress(counts: dict[str, int]) -> None:
            nonlocal seg_discovered, reported, last_report_t, rejected
            seg_discovered = counts["discovered"]
            now = time.monotonic()
            if now - last_report_t < 5.0:
                return
            last_report_t = now
            delta = seg_discovered - reported
            if delta:
                # 거부(세대 교체/일시정지)면 로컬 누계를 전진시키지 않고 발견을 멈춘다.
                if mj.report(discovered=delta):
                    reported = seg_discovered
                else:
                    rejected = True

        # ponytail: 발견 단계는 정체 워치독을 걸지 않는다 — discover_segment 가 세그먼트 단위
        # 블로킹이라 beat 가 세그먼트 완료 후에만 오고, 등록처 레이트리밋 세그먼트는 정상이어도
        # 수백~수천 초라 stall-exit 이 정상 실행을 죽인다(리뷰 HIGH). 발견 정체는 supervisor 가
        # progress_at 로 감시한다(PR4). 워치독은 promote 배치(회사 단위 beat)에만 적용.
        run_pipeline(
            segments, settings=settings, persist=True, record_only=True,
            on_progress=_on_progress, should_cancel=lambda: rejected or should_stop(),
        )
        if rejected or mj.should_stop():  # 협조 중단된 발견을 promote 로 고착시키지 않는다.
            typer.echo("[segment-run] 발견 중 중지 신호 — promote 전이 없이 종료.")
            return
        tail = seg_discovered - reported
        if tail and not mj.report(discovered=tail):
            _rejected()
            return
        remaining = count_promote_targets(
            sm, ctys, industries=inds, listed=listed, regions=regs
        )
        # initial_target 은 이 전이에서 확정(진행률 분모 — 적재 시점엔 알 수 없다).
        if not mj.report(stage="promote", remaining=remaining, initial_target=remaining):
            _rejected()
            return
        typer.echo(
            f"[segment-run] 발견 완료(신규 {seg_discovered}) — promote 진입(대상 {remaining})"
        )
    else:
        remaining = count_promote_targets(
            sm, ctys, industries=inds, listed=listed, regions=regs
        )

    run = PromoteRun.open(settings)
    try:
        with sm() as s:
            guards = _load_domain_guards(s)
        after = row.promote_cursor or ""  # 커서를 쓰는 프로세스는 자기 자신뿐 — 재조회 불요.
        batches_done = 0
        while True:
            if should_stop():
                typer.echo("[segment-run] 중지 신호 감지(취소/세대 교체/일시정지) — 정상종료.")
                return
            rows, last_key, promoted, emails, failed = promote_batch(
                settings, sm, run=run, after=after, limit=batch, workers=workers,
                guards=guards, countries=ctys, industries=inds, listed=listed,
                regions=regs, stall_exit_s=stall_s,
            )
            if rows == 0:
                if not mj.report(stage="done", remaining=0):
                    _rejected()
                    return
                typer.echo("[segment-run] 승격 대상 소진 — 작업 완료.")
                return
            after = last_key
            # ponytail: remaining 은 로컬 감산 근사(배치당 원장 전체 COUNT 회피 — 표시 전용이라
            # fill 의 min_queue 게이팅 같은 정밀 수요 없음). 세대 교체 시 재count 로 보정된다.
            remaining = max(0, remaining - rows)
            if not mj.report(
                processed=rows, promoted=promoted, emails=emails, failed_items=failed,
                batches=1, cursor=after, remaining=remaining,
            ):
                _rejected()
                return
            typer.echo(
                f"[segment-run] 배치 처리 {rows}, 승격 {promoted}, 이메일 {emails}, 실패 {failed}"
            )
            batches_done += 1
            if batches_done >= max_batches:
                typer.echo(f"[segment-run] max_batches={max_batches} 도달 — 정상종료(세대교체).")
                return
    finally:
        run.close()


@app.command("nps-import")
def nps_import(
    path: str = typer.Argument(..., help="국민연금 가입 사업장 월간 CSV 경로(data.go.kr 파일)"),
) -> None:
    """국민연금 사업장 스냅샷을 통째 적재한다(기존 적재분 교체 — 월간 파일 갱신 시 재실행).

    적재분은 NpsSource(발견 소스)가 업종 접두 매칭 + 가입자수 내림차순(대형 우선)으로
    소비한다. 인코딩(utf-8/cp949)은 자동 판별. API 자동 수집은 ``nps-sync``.
    """
    from .storage.db import get_sessionmaker
    from .storage.nps import ingest_nps_csv

    inserted, skipped = ingest_nps_csv(get_sessionmaker(), path)
    typer.echo(f"적재 {inserted:,}행, 건너뜀 {skipped:,}행")
    if inserted == 0:
        raise typer.Exit(code=1)


@app.command("nps-sync")
def nps_sync(
    per_page: int = typer.Option(1000, help="API 페이지 크기(odcloud perPage)"),
    max_pages: int = typer.Option(2000, help="페이지 상한(폭주 방지 — 1000행×2000=200만행 여유)"),
) -> None:
    """국민연금 사업장 스냅샷을 odcloud 오픈API 로 자동 수집·적재한다(수동 다운로드 불필요).

    data.go.kr 파일데이터(15083277)는 월별 업로드마다 새 uddi API 경로가 생긴다 —
    스웨거 문서에서 최신 경로를 골라 페이지 순회 후 통째 교체 적재한다(멱등).
    LEADCRAWLER_DATA_GO_KR_SERVICE_KEY(활용신청 후 발급) 필요. 매달 1회 실행(또는 스케줄).
    """
    import re as _re

    from .config import get_settings
    from .sources.http import Fetcher
    from .storage.db import get_sessionmaker
    from .storage.nps import ingest_nps_rows

    settings = get_settings()
    key = settings.data_go_kr_service_key
    if not key:
        typer.echo("LEADCRAWLER_DATA_GO_KR_SERVICE_KEY 가 없습니다(활용신청 후 .env 에 추가).")
        raise typer.Exit(code=1)

    def _redact(text: str) -> str:
        """예외/URL 문자열에서 serviceKey 를 가린다 — httpx 예외가 전체 URL 을 포함해
        키가 로그로 새는 것을 차단(적대 리뷰 M4)."""
        return text.replace(key, "***")

    fetcher = Fetcher(
        user_agent=settings.discovery_user_agent,
        min_interval=settings.http_request_delay,
        timeout=settings.http_timeout,
    )
    try:
        # 최신 월 경로 선택(적대 리뷰 H3 반영) — 날짜는 **요약(summary)에서만** 추출
        # (경로의 uddi UUID 숫자열이 lexicographic max 를 오염시키던 결함). 6~8자리
        # 숫자를 int 로 비교하고, 요약에 데이터셋 키워드가 있는 경로만 후보로 삼는다.
        docs = fetcher.get_json(
            "https://infuser.odcloud.kr/oas/docs", params={"namespace": "15083277/v1"}
        )
        paths = docs.get("paths") if isinstance(docs, dict) else None
        if not isinstance(paths, dict) or not paths:
            typer.echo("스웨거 문서에서 API 경로를 찾지 못했습니다.")
            raise typer.Exit(code=1)

        def _summary(spec: object) -> str:
            get_spec = spec.get("get") if isinstance(spec, dict) else None
            return str(get_spec.get("summary") or "") if isinstance(get_spec, dict) else ""

        candidates: list[tuple[int, str, str]] = []  # (날짜, 경로, 요약)
        for path, spec in paths.items():
            summary = _summary(spec)
            if "사업장" not in summary and "가입" not in summary:
                continue  # 네임스페이스에 섞인 다른 데이터셋 방어.
            dates = [int(d) for d in _re.findall(r"\d{6,8}", summary)]
            if dates:
                # 8자리(YYYYMMDD)와 6자리(YYYYMM)를 같은 자릿수로 정규화해 비교.
                candidates.append((max(d if d >= 10**7 else d * 100 for d in dates), path, summary))
        if not candidates:
            typer.echo("요약에 날짜가 있는 사업장 데이터셋 경로를 찾지 못했습니다(스웨거 형식 변경?).")
            raise typer.Exit(code=1)
        _, latest_path, summary = max(candidates)
        typer.echo(f"최신 데이터셋: {summary}")

        def _pages():
            page = 1
            while page <= max_pages:
                payload = fetcher.get_json(
                    f"https://api.odcloud.kr/api{latest_path}",
                    params={"page": page, "perPage": per_page, "serviceKey": key},
                )
                data = payload.get("data") if isinstance(payload, dict) else None
                if not data:
                    return
                yield from (d for d in data if isinstance(d, dict))
                if len(data) < per_page:
                    return
                page += 1

        inserted, skipped = ingest_nps_rows(get_sessionmaker(), _pages())
    except typer.Exit:
        raise
    except Exception as exc:  # 키 누출 차단 — 원문 대신 마스킹 문자열로 보고.
        typer.echo(f"동기화 실패: {_redact(str(exc))}")
        raise typer.Exit(code=1) from None
    finally:
        fetcher.close()
    typer.echo(f"적재 {inserted:,}행, 건너뜀 {skipped:,}행")
    if inserted == 0:
        typer.echo("0행 — 활성 스냅샷은 교체하지 않았습니다.")
        raise typer.Exit(code=1)


@app.command("dart-cache-fill")
def dart_cache_fill(
    max_calls: int = typer.Option(0, help="이번 실행 호출 상한(0=일일예산까지)"),
) -> None:
    """DART 전 법인 명부를 corp 캐시에 선적재한다(미스만, 일일예산 내·재개 가능).

    전 법인 ~11.8만 × corp당 1콜 — 일일예산 60k(20k×3키)면 이틀에 전수 완료.
    완료 후엔 발견·조인(NPS 등)이 캐시만으로 홈페이지·사업자번호·상장 필드를 얻는다.
    기존 캐시 행의 조인 컬럼(bizno/name_norm)도 로컬 재계산으로 백필한다(API 0콜).
    """
    from .config import get_settings
    from .sources.dart import DartSource, _FetchedCorp, _quota_key, _QUOTA_SOURCE
    from .sources.registry import close_sources
    from .storage.dart_cache import DbDartCorpCache
    from .storage.db import get_sessionmaker
    from .storage.discovery_cursor import DbCursorStore

    settings = get_settings()
    sm = get_sessionmaker()
    cache = DbDartCorpCache(sm)
    cursor_store = DbCursorStore(sm)

    backfilled = cache.backfill_join_cols()
    if backfilled:
        typer.echo(f"조인 컬럼 백필: {backfilled:,}행 (API 0콜)")

    src = DartSource(settings, cursor_store=cursor_store, corp_cache=cache)
    keys = src._api_keys()
    if not keys:
        typer.echo("DART 키가 없습니다.")
        raise typer.Exit(code=1)
    budget = settings.dart_daily_call_budget

    def _headroom(key: str) -> int:
        if not budget:
            return 10**9
        used = cursor_store.get(_QUOTA_SOURCE, _quota_key(key))
        return max(0, budget - used)

    live = [k for k in keys if _headroom(k) > 0]
    if not live:
        typer.echo("일일예산 소진 — KST 자정 리셋 후 재실행하세요.")
        raise typer.Exit(code=1)

    corps, status = src._fetch_corps(live[0])
    cursor_store.increment(_QUOTA_SOURCE, _quota_key(live[0]), 1)
    if not corps:
        typer.echo(f"corpCode 목록 수집 실패(status={status}).")
        raise typer.Exit(code=1)
    typer.echo(f"전 법인 {len(corps):,} — 캐시 미스만 채웁니다.")

    fetcher = src._client()
    filled = errors = 0
    spent_this = 0
    ki = 0
    batch: list[_FetchedCorp] = []
    try:
        for i in range(0, len(corps), 1000):
            window = corps[i : i + 1000]
            cached = cache.get_many([c for c, _ in window])
            for corp_code, corp_name in window:
                if corp_code in cached:
                    continue
                if max_calls and spent_this >= max_calls:
                    raise StopIteration
                # 예산 남은 키 선택(라운드로빈) — 전부 소진이면 중단(재개 가능).
                tries = 0
                while tries < len(live) and _headroom(live[ki % len(live)]) <= 0:
                    ki += 1
                    tries += 1
                key = live[ki % len(live)]
                if _headroom(key) <= 0:
                    raise StopIteration
                ki += 1
                spent_this += 1
                cursor_store.increment(_QUOTA_SOURCE, _quota_key(key), 1)
                try:
                    info = fetcher.get_json(
                        "https://opendart.fss.or.kr/api/company.json",
                        params={"crtfc_key": key, "corp_code": corp_code},
                    )
                except Exception:
                    errors += 1
                    continue
                st = info.get("status") if isinstance(info, dict) else None
                if st in {"010", "011", "012", "020", "800", "901"}:
                    # 치명 status — 공유 일일카운터는 **진짜 쿼터소진(020)만** 영속 소진
                    # 처리한다. 800(시스템 점검)·키오류는 일시적/키한정이라 CLI 로컬
                    # dead 처리만 — 종일 카운터를 오염시켜 라이브 크롤의 그 키까지
                    # 죽이는 부작용 방지(리뷰 M2).
                    if st == "020":
                        used = cursor_store.get(_QUOTA_SOURCE, _quota_key(key))
                        if budget and used < budget:
                            cursor_store.increment(
                                _QUOTA_SOURCE, _quota_key(key), budget - used
                            )
                    else:
                        live = [k for k in live if k != key]
                        if not live:
                            typer.echo(f"전 키 치명 status({st}) — 중단(재개 가능).")
                            raise StopIteration
                    continue
                if isinstance(info, dict) and st:
                    batch.append(
                        _FetchedCorp(
                            corp_code, corp_name, str(st), info if st == "000" else None
                        )
                    )
                    filled += 1
                if len(batch) >= 500:
                    cache.put_many(batch)
                    batch = []
    except StopIteration:
        pass
    finally:
        if batch:
            cache.put_many(batch)
        # DartSource 엔 close() 가 없다 — 내부 fetcher 만 best-effort 로 닫는 공용 헬퍼 사용.
        close_sources([src])
    typer.echo(f"적재 {filled:,} · 오류 {errors:,} · 이번 호출 {spent_this:,} (재개 가능 — 미스만 재시도)")


@app.command("nps-relink-dart")
def nps_relink_dart(
    dry: bool = typer.Option(False, "--dry", help="변경 없이 대상만 보고"),
) -> None:
    """기존 NPS name: 원장 행을 DART 캐시 조인으로 reg: 키에 재연결한다(제약① 정합).

    조인(#231)이 켜지기 **전에** NPS 가 name: 키로 적재한 행은, 캐시가 채워진 뒤
    같은 회사가 reg:dart: 키로 재발견되며 재추출된다(적대 리뷰 H1) — 이 커맨드가
    같은 정밀 매치(사업자번호 앞6+정규화명)로 키를 선제 재연결해 그 창을 닫는다.
    reg: 키가 이미 원장에 있으면 보존·보고만(과거 중복 — dedup-report 대상).
    재연결 행은 캐시 원문(corp_cls)으로 상장여부·시장·registry 도 함께 채운다
    (미상 전용 — save_discovered 백필과 동일 규약). ``dart-cache-fill`` 완료 후 1회 실행."""
    from sqlalchemy import text as _text

    from .dedup import canonical_key as _ckey, normalize_name as _norm
    from .sources.dart import _LISTED_CLS, _MARKET_CLS
    from .schema import DiscoveredCompanyRow
    from .storage.dart_cache import DbDartCorpCache
    from .storage.db import get_sessionmaker

    sm = get_sessionmaker()
    cache = DbDartCorpCache(sm)
    cols = [c.name for c in DiscoveredCompanyRow.__table__.columns]
    tail = [c for c in cols if c != "canonical_key"]
    copy_sql = _text(
        f"insert into discovered_company (canonical_key, {', '.join(tail)}) "
        f"select :new, {', '.join(tail)} from discovered_company where canonical_key = :old"
    )

    relinked = conflicted = nomatch = 0
    with sm() as s:
        rows = s.execute(
            _text(
                "select dc.canonical_key, dc.name, dc.country, nw.bizno_prefix "
                "from discovered_company dc "
                "join nps_workplace nw on nw.name = dc.name and not nw.pending "
                "where dc.source = 'nps' and dc.canonical_key like 'name:%'"
            )
        ).fetchall()
    targets = {(r.canonical_key): r for r in rows}  # 동명 다지점은 1행(정밀 매치가 거름).
    pairs = [
        ((r.bizno_prefix or "")[:6], _norm(r.name)[:255]) for r in targets.values()
    ]
    matches = cache.find_matches([p for p in pairs if p[0] and p[1]])
    if matches is None:  # 조회 실패(미스 아님) — 오판 재연결·허위 nomatch 방지.
        typer.echo("DART 캐시 조회 실패 — 재연결 중단(재실행하세요)")
        raise typer.Exit(1)
    with sm() as s:
        for old_key, r in targets.items():
            hit = matches.get(((r.bizno_prefix or "")[:6], _norm(r.name)[:255]))
            if hit is None:
                nomatch += 1
                continue
            new_key = _ckey(
                registry="dart", registry_id=hit.corp_code, domain=None,
                name=r.name, country=r.country,
            )
            exists = s.execute(
                _text("select 1 from discovered_company where canonical_key=:k"),
                {"k": new_key},
            ).first()
            if exists:
                conflicted += 1
                typer.echo(f"충돌(보존): {old_key} → {new_key}")
                continue
            if dry:
                relinked += 1
                continue
            # PK 교체는 copy→FK전환→delete (company FK 가 즉시검사라 in-place 불가).
            s.execute(copy_sql, {"new": new_key, "old": old_key})
            # 캐시 원문으로 미상 필드 채움 — NPS 행은 발견 시점 캐시 미비로 listed
            # unknown 박제가 대부분이라, 재연결하면서 corp_cls 를 바로 붙인다.
            corp_cls = str((hit.info or {}).get("corp_cls") or "")
            s.execute(
                _text(
                    # unlisted 도 교정 대상 — name: 키 NPS 행의 unlisted 는 전부 조인 미스
                    # 기본값(실측 아님)이라, 캐시 완충 후 corp_cls 실측으로 덮는 게 옳다
                    # (corp_cls 히트 행은 reg: 키라 여기 안 옴 — 실측 unlisted 는 안 덮인다).
                    "update discovered_company set "
                    "listed = case when listed is null or listed in ('', 'unknown', 'unlisted') "
                    "  then coalesce(:listed, listed) else listed end, "
                    "market = coalesce(market, :market), "
                    "registry = coalesce(registry, 'dart'), "
                    "registry_id = coalesce(registry_id, :corp) "
                    "where canonical_key = :k"
                ),
                {
                    "listed": _LISTED_CLS.get(corp_cls),
                    "market": _MARKET_CLS.get(corp_cls),
                    "corp": hit.corp_code,
                    "k": new_key,
                },
            )
            s.execute(
                _text("update company set canonical_key=:new where canonical_key=:old"),
                {"new": new_key, "old": old_key},
            )
            s.execute(
                _text("delete from discovered_company where canonical_key=:old"),
                {"old": old_key},
            )
            relinked += 1
        s.commit()
    typer.echo(
        f"{'(dry) ' if dry else ''}재연결 {relinked:,} · 충돌보존 {conflicted:,}"
        f" · 캐시매치없음 {nomatch:,} / 대상 {len(targets):,}"
    )


@app.command("nps-map-industries")
def nps_map_industries() -> None:
    """스냅샷의 업종코드(~1.6천)를 택소노미 전량으로 통합 매핑한다(3층 — 코드 단위 1회).

    전 코드 LLM(닫힌 택소노미 분류기 — 셋 밖 값 불가, abstain=미분류). NPS 코드는 KSIC
    10차가 아니어서 10차 접두 규칙 매핑은 전면 오라벨 사고(2026-07-14)로 폐지됐다.
    멱등: 이미 매핑된 코드는 건너뛴다(월간 재실행 시 신규 코드만).
    실행 후 NpsSource 가 매핑 라벨 전체(사각 63% 포함)를 발견 대상으로 연다.
    """
    from .config import get_settings
    from .cost_ledger import CostLedger
    from .enrich.industry_classify import build_classifier
    from .storage.db import get_sessionmaker
    from .storage.nps import map_industry_codes

    settings = get_settings()
    classifier = build_classifier(settings, ledger=CostLedger(settings, persist=True))
    stats = map_industry_codes(get_sessionmaker(), classifier)
    typer.echo(
        f"LLM {stats['llm']:,} · 미분류 {stats['unclassified']:,}"
        f" · 일시실패 재시도대기 {stats['skipped']:,} · 기매핑 {stats['already']:,}"
    )


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


def confirmed_pairs_from_report(
    data: dict, *, include_llm: bool, min_confidence: float
) -> list[tuple[str, str]]:
    """리포트(dict)에서 **확정 중복 쌍**을 수집한다(머지 입력). 결정적·순수.

    - 확정 티어(reg_no=등록번호 일치, auto=이름高+도메인일치)는 항상 포함.
    - ``include_llm`` 이면 LLM 판정도 포함하되 ① same=True ② confidence>=임계 ③ **비-스텁**만.
      스텁(dry_run/키없음)은 도메인root 동일이면 무조건 same 이라 실제 머지 근거로 쓰면
      공유호스팅·별개 사업부를 오병합한다(제약② — 확실치 않으면 보존). key_a<key_b 보장됨.
    """
    from .dedup_resolve.near_dup import CONFIRMED_TIERS

    pairs: list[tuple[str, str]] = [
        (c["key_a"], c["key_b"])
        for c in data.get("candidates", [])
        if c.get("tier") in CONFIRMED_TIERS
    ]
    if include_llm:
        for j in data.get("judged", []):
            v = j.get("verdict", {})
            if v.get("same") and v.get("confidence", 0) >= min_confidence and v.get("model") != "stub":
                pairs.append((j["candidate"]["key_a"], j["candidate"]["key_b"]))
    return pairs


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
    min_confidence: float = typer.Option(
        0.8, help="--include-llm 시 LLM same 채택 최소 confidence(미만은 워크벤치 위임, 제약②)"
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
    pairs = confirmed_pairs_from_report(data, include_llm=include_llm, min_confidence=min_confidence)
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
            # 머지 주체 audit — LLM 판정을 포함했으면 자동(auto)과 구분(롤백 선택성).
            actor = "auto+llm" if include_llm else "auto"
            applied = sum(apply_golden(session, g, merged_by=actor) for g in goldens)
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


@app.command("purge-dead-sites")
def purge_dead_sites(
    apply: bool = typer.Option(False, "--apply", help="실제 반영(미지정=드라이런 카운트만)"),
) -> None:
    """사이트 미생존(site_alive=False)인데 저장된 회사를 비활성화하고 대기 큐에서 뺀다.

    구 동작(등록처 active override)으로 새어 들어온 죽은·406·파킹 사이트 회사를 새 게이트
    (제약 ②: active + 도메인 생존)에 맞춰 정리한다. ``is_active=False`` 로 내려(재-enqueue
    방지) 대기(pending) 검증 큐 행을 제거한다 — 확정·거부분은 감사 위해 보존. 멱등.
    """
    from sqlalchemy import delete, func, select, update

    from .schema import CompanyRow, ReviewQueueRow
    from .storage.db import get_sessionmaker

    configure_logging()
    session = get_sessionmaker(get_settings())()
    try:
        dead_q = select(CompanyRow.id).where(
            CompanyRow.site_alive.is_(False), CompanyRow.is_active.is_(True)
        )
        n_comp = session.scalar(select(func.count()).select_from(dead_q.subquery())) or 0
        n_pending = (
            session.scalar(
                select(func.count())
                .select_from(ReviewQueueRow)
                .where(ReviewQueueRow.company_id.in_(dead_q), ReviewQueueRow.status == "pending")
            )
            or 0
        )
        typer.echo(f"대상: 사이트死 회사 {n_comp:,}곳 / 대기 큐 {n_pending:,}건")
        if not apply:
            typer.echo("드라이런(미반영) — 실제 반영하려면 --apply")
            return
        # 큐 행 삭제를 먼저(dead_q 가 is_active=True 를 참조) → 그 다음 회사 비활성화.
        session.execute(
            delete(ReviewQueueRow).where(
                ReviewQueueRow.company_id.in_(dead_q), ReviewQueueRow.status == "pending"
            )
        )
        session.execute(
            update(CompanyRow)
            .where(CompanyRow.site_alive.is_(False), CompanyRow.is_active.is_(True))
            .values(is_active=False)
        )
        session.commit()
        typer.echo(f"정리 완료: {n_comp:,}곳 비활성화 + 대기 큐 {n_pending:,}건 제거")
    finally:
        session.close()


def backfill_industries(
    session, classifier, *, fetch_html, limit: int = 0, commit_every: int = 50
) -> tuple[int, int]:
    """'미분류'·catch-all 구분의 실존 회사를 재분류해 갱신한다 — (검토, 갱신) 건수 반환.

    파이프라인 유입 시점과 같은 규칙(AMBIGUOUS_LABELS → 분류기, abstain=원래값 유지)을
    기존 행에 소급 적용한다. 홈페이지 본문(``fetch_html``)이 있을 때만 분류한다 — 없으면
    (홈페이지 없음·fetch 실패) 이름만 블라인드 분류(오라벨·과금)하지 않고 스킵한다
    (파이프라인 홈페이지 게이트와 동일 규칙). 닫힌 택소노미 밖 값은 절대 쓰지 않고
    abstain 은 원래값을 유지하므로 반복 실행해도 안전하다(멱등).

    ``commit_every`` 건마다 중간 커밋한다 — 전체 런은 행당 유료 호출이 있어, 중단 시
    전량 롤백이면 그만큼의 LLM 지출이 통째로 증발한다(0=끄기, 마지막 커밋은 호출부).
    """
    from sqlalchemy import select

    from .schema import CompanyRow, DiscoveredCompanyRow
    from .sources.taxonomy import AMBIGUOUS_LABELS

    stmt = (
        select(CompanyRow, DiscoveredCompanyRow.domain)
        .join(
            DiscoveredCompanyRow,
            DiscoveredCompanyRow.canonical_key == CompanyRow.canonical_key,
        )
        .where(CompanyRow.is_active.is_(True), CompanyRow.industry.in_(AMBIGUOUS_LABELS))
        .order_by(CompanyRow.id)
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = session.execute(stmt).all()
    updated = 0
    for i, (company, domain) in enumerate(rows, start=1):
        html = fetch_html(company.homepage) if company.homepage else None
        # 홈페이지 게이트(파이프라인 run.py 와 동일): 본문 없으면 이름만 블라인드 분류로
        # 오라벨(자동차 편중)·과금하지 않고 스킵 — 미분류 유지, 다음 실행에서 재시도(멱등).
        if not html:
            continue
        # 분류기는 계약상 실패를 abstain(None)으로 흡수한다 — 확신 라벨일 때만 갱신.
        verdict = classifier.classify(company.name, domain, html)
        if verdict.label and verdict.label != company.industry:
            company.industry = verdict.label
            updated += 1
        if commit_every and i % commit_every == 0:
            session.commit()  # 중단돼도 여기까지의 재분류(=지출)는 살린다.
    return len(rows), updated


def fetch_industry_html(url: str, *, get, render) -> str | None:
    """업종 백필용 홈페이지 본문 확보 — 폴백 사다리(www → http → 헤드리스). 실패=None.

    ``get(url)`` 은 httpx 응답(status_code·text), ``render(url)`` 은 헤드리스 렌더 HTML
    (실패 None)을 돌려주는 주입 함수(테스트=스텁). 2026-08-26 잔여 미분류 표본 40곳 실측:
    - www 폴백(기존): 루트 무응답·www 만 서빙하는 사이트 회수.
    - **http 폴백**: https 접속 자체가 실패(ConnectError — 인증서 만료·https 미서빙)한 호스트만
      http:// 로 1회 더 — ConnectError 12곳 중 3곳 회수. 4xx/5xx 응답을 받은 호스트는 안 한다.
    - **헤드리스**: 403 은 대부분 봇차단(Cloudflare 등)이라 실브라우저는 통과 — 9곳 중 6곳 회수.
      406(KR 호스팅 차원 차단)은 브라우저도 막혀 제외. 미통과 챌린지 페이지는 본문으로 안 친다.
    """
    scheme, sep, host = url.partition("://")
    if not sep:
        scheme, host = "https", url
    hosts = [host] if host.startswith("www.") else [host, f"www.{host}"]
    blocked = False
    for h in hosts:
        for s in dict.fromkeys((scheme, "http")):
            try:
                r = get(f"{s}://{h}")
            except Exception:  # 접속 실패 → 다음 스킴/호스트.
                continue
            if r.status_code < 400 and r.text:
                return r.text
            blocked = blocked or r.status_code == 403
            break  # 응답은 받았다(4xx/5xx) — 같은 호스트의 다른 스킴은 안 본다.
    if blocked:
        html = render(url) or ""
        low = html[:4000].lower()
        if html and "just a moment" not in low and "cf-chl" not in low:
            return html
    return None


@app.command("backfill-industry")
def backfill_industry(
    limit: int = typer.Option(0, help="처리 상한(0=전체) — 소량 시험용"),
) -> None:
    """'미분류'·catch-all 구분으로 남은 기존 회사를 LLM 으로 소급 재분류한다(멱등).

    파이프라인은 유입 시점에만 분류하므로, 그때 보류(abstain — fetch 실패·429 등)된
    행은 이 명령으로 재시도한다. 홈페이지 없는 행은 여기서도 분류하지 않는다(블라인드
    분류 금지 — 홈페이지가 생기면 그때 재분류). dry_run/키없음이면 무료 키워드 스텁으로 동작하고,
    라이브는 cost_ledger 월예산·런당캡 가드 안에서만 과금 호출한다.
    """
    import httpx

    from .cost_ledger import CostLedger
    from .enrich.industry_classify import build_classifier
    from .storage.db import get_sessionmaker

    configure_logging()
    settings = get_settings()
    ledger = CostLedger(settings, persist=not settings.dry_run)
    classifier = build_classifier(settings, ledger=ledger)
    client = httpx.Client(
        timeout=10,
        follow_redirects=True,
        headers={"User-Agent": settings.discovery_user_agent},
    )

    from .enrich.headless import PlaywrightRenderer

    renderer = PlaywrightRenderer(timeout=settings.headless_timeout)

    def fetch_html(url: str) -> str | None:
        if classifier.model == "stub":  # dry_run/키없음 — 네트워크 0 계약(§2) 유지.
            return None
        return fetch_industry_html(url, get=client.get, render=renderer.render)

    session = get_sessionmaker(settings)()
    try:
        examined, updated = backfill_industries(
            session, classifier, fetch_html=fetch_html, limit=limit
        )
        session.commit()
    finally:
        session.close()
        client.close()
        renderer.close()
    mode = "스텁(무과금)" if classifier.model == "stub" else f"LLM({classifier.model})"
    typer.echo(
        f"구분 백필 완료[{mode}]: 검토 {examined}건 → 재분류 {updated}건 "
        f"(보류 {examined - updated}건은 원래값 유지)"
    )


def backfill_dart_markets(
    session, get_info, *, limit: int = 0, commit_every: int = 50
) -> tuple[int, int]:
    """DART 원장 행의 상장여부(listed)·시장 보드(market)를 corp_cls 로 소급 기입한다
    — (검토, 갱신) 건수 반환.

    대상: registry='dart' 이고 ① listed='unknown'(#130 corp_cls 세분화 이전 코드가 남긴
    잔재) 또는 ② listed='listed' 인데 market 미상. ``get_info(corp_code)`` 는 DART
    company.json 응답 dict(실패/미확인=None)를 돌려주는 주입 함수(테스트=스텁, 라이브=API).
    corp_cls 를 못 받으면 원래값을 유지하므로 반복 실행해도 안전하다(멱등).
    ``commit_every`` 건마다 중간 커밋한다(중단 시 진행분 보존, 0=끄기).
    """
    from sqlalchemy import and_, or_, select

    from .schema import DiscoveredCompanyRow
    from .sources.dart import _LISTED_CLS, _MARKET_CLS

    stmt = (
        select(DiscoveredCompanyRow)
        .where(
            DiscoveredCompanyRow.registry == "dart",
            DiscoveredCompanyRow.registry_id.is_not(None),
            or_(
                DiscoveredCompanyRow.listed == "unknown",
                and_(
                    DiscoveredCompanyRow.listed == "listed",
                    DiscoveredCompanyRow.market.is_(None),
                ),
            ),
        )
        .order_by(DiscoveredCompanyRow.canonical_key)
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = session.scalars(stmt).all()
    updated = 0
    for i, row in enumerate(rows, start=1):
        info = get_info(row.registry_id)
        cls = (info or {}).get("corp_cls", "")
        new_listed = _LISTED_CLS.get(cls)
        if new_listed is None:  # 응답 실패/미지 corp_cls → 원래값 유지(멱등·보수적).
            continue
        new_market = _MARKET_CLS.get(cls)
        if (row.listed, row.market) != (new_listed, new_market):
            row.listed = new_listed
            row.market = new_market
            updated += 1
        if commit_every and i % commit_every == 0:
            session.commit()  # 중단돼도 여기까지의 기입(=API 콜 소비분)은 살린다.
    return len(rows), updated


@app.command("backfill-market")
def backfill_market(
    limit: int = typer.Option(0, help="처리 상한(0=전체) — 소량 시험용"),
) -> None:
    """DART 원장 행의 상장여부·시장 보드를 corp_cls 로 소급 기입한다(멱등, 무료 쿼터).

    corp_cls 세분화(#130) 이전 코드가 남긴 listed='unknown' 잔재와 market 미상 상장행을
    company.json 재조회로 채운다. 행당 1콜(무료 쿼터) — DRY-RUN/키 없음이면 무과금
    계약(§2)에 따라 아무것도 하지 않는다.
    """
    from .sources.dart import _COMPANY_URL
    from .sources.http import Fetcher
    from .storage.db import get_sessionmaker

    configure_logging()
    settings = get_settings()
    if settings.dry_run or not settings.dart_api_key:
        typer.echo("DRY-RUN 또는 DART 키 없음 — 네트워크 0 계약에 따라 건너뜀")
        raise typer.Exit(0)
    fetcher = Fetcher(
        min_interval=settings.http_request_delay, timeout=settings.http_timeout
    )

    def get_info(corp_code: str) -> dict | None:
        try:
            info = fetcher.get_json(
                _COMPANY_URL,
                params={"crtfc_key": settings.dart_api_key, "corp_code": corp_code},
            )
        except Exception:  # 개별 실패는 보류(다음 실행에 재시도) — 배치 보호.
            return None
        return info if isinstance(info, dict) and info.get("status") == "000" else None

    session = get_sessionmaker(settings)()
    try:
        examined, updated = backfill_dart_markets(session, get_info, limit=limit)
        session.commit()
    finally:
        session.close()
    typer.echo(
        f"시장 백필 완료: 검토 {examined}건 → 기입 {updated}건 "
        f"(보류 {examined - updated}건은 원래값 유지, 재실행 시 재시도)"
    )


@app.command("seed-mock")
def seed_mock() -> None:
    """로컬 개발용 목 리드 5건을 DB 에 적재한다(검증 웹앱 둘러보기용 — 멱등).

    docker-compose PG 를 띄우고 ``db-upgrade`` 로 스키마를 적용한 뒤 실행하면, 검증
    워크벤치가 빈 화면 대신 실제 행(단일/다중 후보·해외·문의폼만)을 보여준다. 여러 번
    실행해도 canonical_key 기준 멱등이라 중복이 생기지 않는다.
    """
    from .seed import seed_mock_leads
    from .storage.db import session_scope

    configure_logging()
    with session_scope(get_settings()) as s:
        count = seed_mock_leads(s)
    typer.echo(f"목 리드 {count}건 적재 완료(검증 큐 등록). 웹앱: `leadcrawler web`")


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="바인드 호스트(내부망 공개는 0.0.0.0)"),
    port: int = typer.Option(8000, help="포트"),
    ssl_certfile: str | None = typer.Option(
        None, help="TLS 인증서(PEM) 경로 — 키와 함께 지정하면 HTTPS 로 서빙"
    ),
    ssl_keyfile: str | None = typer.Option(None, help="TLS 개인키(PEM) 경로"),
    reload: bool = typer.Option(
        False, "--reload", help="파이썬 코드 변경 시 자동 재시작(내부망/개발용 — 진행 중 크롤은 끊김)"
    ),
) -> None:
    """검증 웹앱(FastAPI)을 띄운다. fastapi/uvicorn extra(`.[api]`) 필요.

    내부망 HTTPS: `scripts/windows/gen-ssl-cert.ps1` 로 자체서명 인증서를 만든 뒤
    `leadcrawler web --host 0.0.0.0 --ssl-certfile certs/cert.pem --ssl-keyfile certs/key.pem`.
    """
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise typer.BadParameter(
            "uvicorn 이 없습니다. `uv sync --extra api` 로 설치하세요."
        ) from exc

    if (ssl_certfile is None) != (ssl_keyfile is None):
        raise typer.BadParameter("--ssl-certfile 과 --ssl-keyfile 은 함께 지정해야 합니다.")

    configure_logging()
    uvicorn.run(
        "leadcrawler.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        reload=reload,
    )


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
