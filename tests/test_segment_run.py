"""트랙 S 자식 CLI(``segment-run``, 세그먼트 작업 큐 설계 §3·§6 PR3) 계약 검증.

dry_run 기본(네트워크 0) — ``run_pipeline``·``_build_lead``·``promote_batch`` 가 dry_run
에서도 결정적 더미로 동작하므로(설계 §3 "DRY-RUN 게이트 미적용"), 발견→승격→review_queue
적재까지 실제 코드 경로로 검증할 수 있다.
"""

from __future__ import annotations

import pytest
import typer
from sqlalchemy import select

import leadcrawler.cli as cli
import leadcrawler.pipeline.promote as pmod
from leadcrawler.config import Settings
from leadcrawler.pipeline import run_pipeline
from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow, ReviewQueueRow
from leadcrawler.sources.base import Segment
from leadcrawler.storage.backfill_job import (
    activate_segment_job,
    enqueue_segment_job,
    get_backfill_job,
    pause_backfill_job,
    record_progress,
)
from leadcrawler.storage.db import get_sessionmaker, init_db, session_scope

# dry_run 등록처(nps)·검색(naver_local) 더미 소스가 KR/건설·엔지니어링 세그먼트에서 결정적으로
# 내놓는 4개 회사(taxonomy 정규 라벨 — 원문 "건설"은 파이프라인이 이 라벨로 정규화한다).
_INDUSTRY = "건설·엔지니어링"


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    s = Settings(database_url=f"sqlite:///{tmp_path}/sr.db", dry_run=True)
    init_db(s)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    return s


def _enqueue_and_activate(settings: Settings, **kw) -> str:
    sm = get_sessionmaker(settings)
    with sm() as s:
        job = enqueue_segment_job(s, countries="KR", industries=_INDUSTRY, **kw)
        s.commit()
        activate_segment_job(s, job.id)
        s.commit()
        return job.id


def test_full_flow_discover_promote_done_with_review_queue(settings) -> None:
    """''→discover→promote→done 전이, 카운터 누적, review_queue 행 생성(더미 lead 경로)."""
    jid = _enqueue_and_activate(settings, batch=10, workers=1, max_batches=5)
    cli.segment_run(
        job_id=jid, job_generation=0, batch=10, workers=1, max_batches=5, stall_exit_secs=0
    )
    sm = get_sessionmaker(settings)
    with sm() as s:
        job = get_backfill_job(s, jid)
        assert job.stage == "done"
        assert job.remaining == 0 and job.initial_target == 4  # 진행률 분모 확정·완료 시 0.
        assert job.discovered == 4
        assert (job.processed, job.promoted, job.emails, job.failed_items) == (4, 4, 4, 0)
        assert job.promote_cursor is not None
        assert len(s.scalars(select(CompanyRow)).all()) == 4
        assert len(s.scalars(select(ReviewQueueRow)).all()) == 4


def test_max_batches_stops_mid_promote_then_resumes_via_cursor(settings) -> None:
    """--max-batches 도달 시 stage 는 promote 그대로 exit 0 — 재호출이 커서에서 이어받는다."""
    jid = _enqueue_and_activate(settings, batch=2, workers=1, max_batches=1)
    cli.segment_run(
        job_id=jid, job_generation=0, batch=2, workers=1, max_batches=1, stall_exit_secs=0
    )
    sm = get_sessionmaker(settings)
    with sm() as s:
        job = get_backfill_job(s, jid)
        assert job.stage == "promote"  # 세대교체 대기 — 아직 done 아님.
        assert job.processed == 2 and job.promoted == 2
        first_cursor = job.promote_cursor
        assert len(s.scalars(select(CompanyRow)).all()) == 2

    # 재호출(같은 세대) — 커서 이후 나머지 2건만 처리하고 done 으로 마무리.
    cli.segment_run(
        job_id=jid, job_generation=0, batch=2, workers=1, max_batches=5, stall_exit_secs=0
    )
    with sm() as s:
        job = get_backfill_job(s, jid)
        assert job.stage == "done"
        assert job.processed == 4 and job.promoted == 4
        assert job.promote_cursor != first_cursor
        assert len(s.scalars(select(CompanyRow)).all()) == 4


def test_promote_cursor_resume_skips_already_passed_keys(settings) -> None:
    """제약①: promote_cursor 이전 키는 discover 없이도(직접 seed) 재처리되지 않는다."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        for key in ("dom:a1.co.kr", "dom:a2.co.kr", "dom:a3.co.kr", "dom:a4.co.kr"):
            s.add(DiscoveredCompanyRow(
                canonical_key=key, name=key, country="KR", industry="게임",
                source="nps", domain=key.removeprefix("dom:"),
            ))
        s.commit()
        job = enqueue_segment_job(s, countries="KR", industries="게임", batch=10, workers=1)
        s.commit()
        activate_segment_job(s, job.id)
        # 이미 discover 를 거쳐 promote 단계이고, a1·a2 는 이전 세대가 이미 훑은 것으로 간주.
        record_progress(s, job.id, 0, stage="promote", cursor="dom:a2.co.kr")
        s.commit()
        jid = job.id

    cli.segment_run(
        job_id=jid, job_generation=0, batch=10, workers=1, max_batches=5, stall_exit_secs=0
    )
    with sm() as s:
        job = get_backfill_job(s, jid)
        assert job.stage == "done"
        assert job.processed == 2  # a3·a4 만.
        promoted_keys = {
            c.canonical_key for c in s.scalars(select(CompanyRow)).all()
        }
        assert promoted_keys == {"dom:a3.co.kr", "dom:a4.co.kr"}


def test_mid_batch_generation_change_rejects_report_and_preserves_cursor(
    settings, monkeypatch
) -> None:
    """배치 도중 세대가 바뀌면(pause) 보고가 거부되고 즉시 종료 — 커서는 그대로 보존."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        job = enqueue_segment_job(s, countries="KR", industries="게임", batch=10, workers=1)
        s.commit()
        activate_segment_job(s, job.id)
        record_progress(s, job.id, 0, stage="promote", cursor="")
        s.commit()
        jid = job.id

    calls = {"n": 0}

    def fake_promote_batch(settings, sm, *, run, after, limit, workers, guards, **kw):
        calls["n"] += 1
        with sm() as s:
            pause_backfill_job(s, jid)  # 배치 처리 도중 supervisor 가 pause(세대 bump).
            s.commit()
        return 1, "dom:whatever", 1, 0, 0

    monkeypatch.setattr(pmod, "promote_batch", fake_promote_batch)
    cli.segment_run(
        job_id=jid, job_generation=0, batch=10, workers=1, max_batches=5, stall_exit_secs=0
    )
    assert calls["n"] == 1  # 보고 거부 → 재시도 없이 즉시 종료.
    with sm() as s:
        job = get_backfill_job(s, jid)
        assert job.status == "paused"
        assert job.promote_cursor == ""  # 거부된 보고의 커서는 반영되지 않는다.
        assert job.processed == 0


def test_invalid_job_state_exits_before_any_work(settings, monkeypatch) -> None:
    """트랙 불일치·세대 불일치·비 running 상태는 실행 전 exit 1(fail-loud)."""
    from leadcrawler.storage.backfill_job import create_backfill_job

    monkeypatch.setattr(pmod, "promote_batch", lambda *a, **k: (0, "", 0, 0, 0))

    sm = get_sessionmaker(settings)
    kw = dict(batch=10, workers=1, max_batches=5, stall_exit_secs=0)

    # 없는 job.
    with pytest.raises(typer.Exit):
        cli.segment_run(job_id="bf_없음", job_generation=0, **kw)

    # 트랙 불일치(A 잡을 S 커맨드에).
    with sm() as s:
        a = create_backfill_job(s, track="A", countries="KR")
        s.commit()
        aid = a.id
    with pytest.raises(typer.Exit):
        cli.segment_run(job_id=aid, job_generation=0, **kw)

    # queued(아직 activate 안 됨) — running 아니라 거부.
    with sm() as s:
        job = enqueue_segment_job(s, countries="KR", industries="게임")
        s.commit()
        qid = job.id
    with pytest.raises(typer.Exit):
        cli.segment_run(job_id=qid, job_generation=0, **kw)

    # 세대 불일치.
    with sm() as s:
        activate_segment_job(s, qid)
        s.commit()
    with pytest.raises(typer.Exit):
        cli.segment_run(job_id=qid, job_generation=99, **kw)


def test_record_only_writes_ledger_only_no_pending_extraction(tmp_path) -> None:
    """run_pipeline(record_only=True) — 원장만 기록, 회사·연락처(추출)는 생성 안 함."""
    settings = Settings(database_url=f"sqlite:///{tmp_path}/ro.db", dry_run=True)
    init_db(settings)
    leads = run_pipeline(
        [Segment(country="KR", industry="건설")],
        settings=settings, persist=True, record_only=True,
    )
    assert leads == []  # 추출(pending)로 안 넘어가므로 리드 0.
    with session_scope(settings) as s:
        assert len(s.scalars(select(DiscoveredCompanyRow)).all()) == 4  # 원장은 기록됨.
        assert len(s.scalars(select(CompanyRow)).all()) == 0  # 회사행은 없음(제약②는 promote 몫).


def test_discover_stage_recorded_and_pause_mid_discover_skips_promote(
    settings, monkeypatch
) -> None:
    """발견 시작 시 stage='discover' 기록, 발견 중 pause 면 promote 로 전이하지 않고 종료."""
    sm = get_sessionmaker(settings)
    jid = _enqueue_and_activate(settings, batch=10, workers=1)
    seen_stage: list[str] = []

    def fake_run_pipeline(segments, *, on_progress, should_cancel, **kw):
        with sm() as s:
            seen_stage.append(get_backfill_job(s, jid).stage)
            pause_backfill_job(s, jid)  # 발견 도중 supervisor 가 pause(세대 bump).
            s.commit()
        on_progress({"discovered": 3})  # 스로틀 창 안이라 보고 생략 → tail 로 넘어감.
        assert should_cancel() is True  # 다음 세그먼트 전에 협조 중단.
        return []

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)
    cli.segment_run(
        job_id=jid, job_generation=0, batch=10, workers=1, max_batches=5, stall_exit_secs=0
    )
    assert seen_stage == ["discover"]  # 유료 발견 전에 stage 가 기록됐다.
    with sm() as s:
        job = get_backfill_job(s, jid)
        assert job.status == "paused" and job.stage == "discover"  # promote 고착 없음.
        assert job.discovered == 0  # 거부된 보고는 반영되지 않는다.


def test_empty_filters_are_rejected(settings) -> None:
    """빈 countries/industries 는 적재에서 거부되고, DB 이상으로 들어와도 CLI 가 즉시 중단."""
    sm = get_sessionmaker(settings)
    with sm() as s:
        with pytest.raises(ValueError):
            enqueue_segment_job(s, countries="KR", industries="")
        with pytest.raises(ValueError):
            enqueue_segment_job(s, countries="", industries="게임")
    jid = _enqueue_and_activate(settings, batch=10, workers=1)
    with sm() as s:
        get_backfill_job(s, jid).industries = ""  # 검증 우회 시뮬레이션.
        s.commit()
    with pytest.raises(typer.Exit):
        cli.segment_run(
            job_id=jid, job_generation=0, batch=10, workers=1, max_batches=5, stall_exit_secs=0
        )
    with sm() as s:
        assert get_backfill_job(s, jid).stage == ""  # 어떤 단계도 진입하지 않았다.
