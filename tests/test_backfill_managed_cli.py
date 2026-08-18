"""관리형 CLI(--job-id) 계약(#352 PR③) — 진행 자기보고·취소 폴링·트랙 잠금."""

from __future__ import annotations

import pytest

import leadcrawler.cli as cli
import leadcrawler.pipeline.fill as fill
from leadcrawler.config import Settings
from leadcrawler.storage.backfill_job import (
    create_backfill_job,
    get_backfill_job,
    request_cancel,
)
from leadcrawler.storage.db import get_engine, get_sessionmaker, init_db
from leadcrawler.storage.track_lock import acquire_track_lock


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    s = Settings(database_url=f"sqlite:///{tmp_path}/mc.db", dry_run=False)
    init_db(s)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    return s


def _make_job(settings: Settings, track: str) -> str:
    sm = get_sessionmaker(settings)
    with sm() as s:
        job = create_backfill_job(session=s, track=track)
        s.commit()
        return job.id


def test_managed_loop_reports_progress(settings, monkeypatch) -> None:
    """--job-id 루프: 배치마다 backfill_job 카운터가 누적되고 remaining 이 갱신된다."""
    jid = _make_job(settings, "A")
    monkeypatch.setattr(fill, "count_targets", lambda sm, countries=None, **kw: 50)

    def fake_fill_batch(s, sm, *, limit, workers, countries=None, **kw):
        return 20, 2

    monkeypatch.setattr(fill, "fill_batch", fake_fill_batch)
    cli.fill_emails(
        loop=True, batch=5, workers=1, interval=0.0, min_queue=0, max_batches=3,
        job_id=jid, job_generation=0,
    )
    sm = get_sessionmaker(settings)
    with sm() as s:
        job = get_backfill_job(s, jid)
        assert (job.processed, job.emails, job.batches_done) == (60, 6, 3)
        assert job.remaining == 50


def test_managed_loop_stops_on_cancel(settings, monkeypatch) -> None:
    """취소 플래그가 서면 다음 루프 시작 전에 정상종료한다(배치 실행 0)."""
    jid = _make_job(settings, "A")
    sm = get_sessionmaker(settings)
    with sm() as s:
        request_cancel(s, jid)
        s.commit()
    calls = {"n": 0}

    def fake_fill_batch(*a, **k):
        calls["n"] += 1
        return 1, 0

    monkeypatch.setattr(fill, "count_targets", lambda *a, **k: 100)
    monkeypatch.setattr(fill, "fill_batch", fake_fill_batch)
    cli.fill_emails(
        loop=True, batch=5, workers=1, interval=0.0, min_queue=0, max_batches=0,
        job_id=jid, job_generation=0,
    )  # max_batches=0(무제한)이어도 취소로 반환해야 한다 — 아니면 이 테스트는 무한루프.
    assert calls["n"] == 0


def test_resolve_managed_single_batch_reports(settings, monkeypatch) -> None:
    """resolve 단발(--job-id): 처리/해석/승격이 1회 보고된다."""
    settings.resolve_domains = True
    jid = _make_job(settings, "C")
    monkeypatch.setattr(fill, "resolve_batch", lambda *a, **k: (30, 4, 2))
    cli.backfill_resolve_domains(loop=False, batch=5, workers=1, job_id=jid, job_generation=0)
    sm = get_sessionmaker(settings)
    with sm() as s:
        job = get_backfill_job(s, jid)
        assert (job.processed, job.resolved, job.promoted, job.batches_done) == (30, 4, 2, 1)


def test_track_lock_sqlite_noop_and_validation(settings) -> None:
    """SQLite 는 잠금 no-op(항상 성공), 허용 밖 트랙은 fail-loud."""
    engine = get_engine(settings)
    lock = acquire_track_lock(engine, "A")
    assert lock is not None
    lock.close()
    lock.close()  # 멱등.
    with pytest.raises(ValueError):
        acquire_track_lock(engine, "B")


def test_managed_wait_chunks_detect_cancel(settings) -> None:
    """_ManagedJob.wait — 취소 시 조기 True, 비취소면 시간 경과 후 False(청크 로직)."""
    jid = _make_job(settings, "A")
    sm = get_sessionmaker(settings)
    mj = cli._ManagedJob(sm, jid, 0)
    assert mj.wait(0.2) is False  # 비취소 — 대기 완료.
    with sm() as s:
        request_cancel(s, jid)
        s.commit()
    assert mj.wait(60.0) is True  # 취소 — 60초를 기다리지 않고 즉시 반환.


def test_managed_job_validation_fails_loud(settings, monkeypatch) -> None:
    """--job-id 검증 — 트랙 불일치·부재 job 은 실행 전 exit 1(fail-open 차단)."""
    import typer

    jid = _make_job(settings, "C")  # C 트랙 job 을 A 커맨드에 잘못 전달.
    monkeypatch.setattr(fill, "fill_batch", lambda *a, **k: (1, 0))
    with pytest.raises(typer.Exit) as exc:
        cli.fill_emails(loop=False, batch=1, workers=1, job_id=jid, job_generation=0)
    assert exc.value.exit_code == 1
    with pytest.raises(typer.Exit):
        cli.fill_emails(loop=False, batch=1, workers=1, job_id="bf_없는잡", job_generation=0)


def test_report_rejection_stops_loop(settings, monkeypatch) -> None:
    """세대 교체 후 구세대 자식의 보고가 거부되면 루프가 스스로 멈춘다(유령 세대 방지)."""
    from leadcrawler.storage.backfill_job import update_backfill_job

    jid = _make_job(settings, "A")
    sm = get_sessionmaker(settings)
    with sm() as s:
        update_backfill_job(s, jid, generation=1)  # supervisor 가 세대 교체한 상황.
        s.commit()
    calls = {"n": 0}

    def fake_fill_batch(*a, **k):
        calls["n"] += 1
        return 1, 0

    monkeypatch.setattr(fill, "count_targets", lambda *a, **k: 100)
    monkeypatch.setattr(fill, "fill_batch", fake_fill_batch)
    # 구세대(0)로 루프 실행 — 첫 배치 보고가 거부돼 max_batches(0=무제한)와 무관하게 종료.
    cli.fill_emails(
        loop=True, batch=5, workers=1, interval=0.0, min_queue=0, max_batches=0,
        job_id=jid, job_generation=0,
    )
    assert calls["n"] == 1  # 배치 1회 후 보고 거부 → 즉시 정상종료.


def test_lock_busy_exits_with_code_1(settings, monkeypatch) -> None:
    """트랙 잠금 점유 중이면 배치 실행 없이 exit 1 — bat 러너는 60초 후 재시도(무해)."""
    import typer

    import leadcrawler.storage.track_lock as tl

    monkeypatch.setattr(tl, "acquire_track_lock", lambda engine, track: None)
    ran = {"n": 0}
    monkeypatch.setattr(fill, "fill_batch", lambda *a, **k: ran.__setitem__("n", 1) or (1, 0))
    with pytest.raises(typer.Exit) as exc:
        cli.fill_emails(loop=False, batch=1, workers=1)
    assert exc.value.exit_code == 1
    assert ran["n"] == 0  # 잠금 실패 시 대상 처리 0(과금 이중화 없음).


def test_unmanaged_call_skips_reporting(settings, monkeypatch) -> None:
    """--job-id 없으면(비관리형) 보고·취소 폴링이 전혀 안 붙는다(기존 동작 보존)."""
    import leadcrawler.storage.backfill_job as bj

    def boom(*a, **k):
        raise AssertionError("비관리형에서 record_progress 호출됨")

    monkeypatch.setattr(bj, "record_progress", boom)
    monkeypatch.setattr(fill, "fill_batch", lambda *a, **k: (1, 0))
    cli.fill_emails(loop=False, batch=1, workers=1)  # job_id 미지정(OptionInfo) 경로.
