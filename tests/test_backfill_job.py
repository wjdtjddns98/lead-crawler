"""backfill_job 스토리지 계층(#352 PR②) — 활성 1건 가드·원자 진행보고·세대 펜싱."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from leadcrawler.config import Settings
from leadcrawler.storage.backfill_job import (
    CANCELLED,
    FAILED,
    BackfillBusy,
    active_backfill_job,
    backfill_job_dict,
    create_backfill_job,
    finish_backfill_job,
    is_cancel_requested,
    latest_backfill_job,
    record_progress,
    request_cancel,
    update_backfill_job,
)
from leadcrawler.storage.db import init_db, session_scope


@pytest.fixture
def session(tmp_path) -> Session:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/bf.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        yield s


def test_active_track_single_guard(session: Session) -> None:
    """같은 트랙 활성 1건 강제 — 두 번째 시작은 BackfillBusy(활성 id 포함)."""
    job = create_backfill_job(session, track="A", countries="KR", triggered_by="admin")
    assert job.status == "running" and job.active_track == "A"
    with pytest.raises(BackfillBusy) as exc:
        create_backfill_job(session, track="A")
    assert exc.value.active_id == job.id
    # 다른 트랙(C)은 독립 — 동시 실행 허용.
    other = create_backfill_job(session, track="C")
    assert active_backfill_job(session, "C").id == other.id


def test_finish_releases_active_and_allows_next(session: Session) -> None:
    """종료가 active_track 을 비워 다음 작업 시작을 허용한다(멱등 포함)."""
    job = create_backfill_job(session, track="A")
    finish_backfill_job(session, job.id, CANCELLED, stop_reason="operator")
    assert job.active_track is None and job.finished_at is not None
    # 멱등 — 이미 종료된 작업 재종료는 상태를 덮지 않는다.
    again = finish_backfill_job(session, job.id, FAILED, error="late")
    assert again.status == CANCELLED and again.error is None
    # 활성이 비었으니 새 작업 시작 가능.
    nxt = create_backfill_job(session, track="A")
    assert active_backfill_job(session, "A").id == nxt.id
    assert latest_backfill_job(session, "A").id == nxt.id


def test_record_progress_accumulates_and_fences_generation(session: Session) -> None:
    """진행 보고는 원자 누적이고, 세대 불일치(구세대 늦은 보고)는 무시된다."""
    job = create_backfill_job(session, track="A", initial_target=100)
    assert record_progress(session, job.id, 0, processed=200, emails=3, batches=1, remaining=97)
    assert record_progress(session, job.id, 0, processed=200, emails=1, batches=1)
    session.refresh(job)
    assert (job.processed, job.emails, job.batches_done, job.remaining) == (400, 4, 2, 97)
    # 세대 교체 후 구세대(0) 보고는 거부, 신세대(1) 보고만 반영.
    update_backfill_job(session, job.id, generation=1)
    assert not record_progress(session, job.id, 0, processed=999)
    assert record_progress(session, job.id, 1, processed=10)
    session.refresh(job)
    assert job.processed == 410


def test_terminal_rows_coexist_and_block_late_reports(session: Session) -> None:
    """종료행 다중 공존(NULL 유니크 비충돌) + 종료 후 같은 세대 늦은 보고 차단."""
    a = create_backfill_job(session, track="A")
    c = create_backfill_job(session, track="C")
    finish_backfill_job(session, a.id, CANCELLED)
    finish_backfill_job(session, c.id, FAILED, error="x")
    # NULL≠NULL — active_track 유니크 제약 아래 종료행 여러 건이 공존한다(운영 정상상태).
    assert a.active_track is None and c.active_track is None
    a2 = create_backfill_job(session, track="A")
    finish_backfill_job(session, a2.id, CANCELLED)  # 같은 트랙 종료행 2건째도 무충돌.
    # 종료 후 같은 세대(0)의 늦은 자기보고는 거부 — 닫힌 통계 불변.
    assert not record_progress(session, a.id, 0, processed=999)
    session.refresh(a)
    assert a.processed == 0


def test_cancel_flag_and_dict(session: Session) -> None:
    """취소는 플래그만 세우고(협조적), dict 스냅샷은 평탄·ISO 시각이다."""
    job = create_backfill_job(session, track="C", exclude_industries="은행,보험")
    assert not is_cancel_requested(session, job.id)
    request_cancel(session, job.id)
    assert is_cancel_requested(session, job.id)
    d = backfill_job_dict(job)
    assert d["track"] == "C" and d["exclude_industries"] == "은행,보험"
    assert isinstance(d["started_at"], str)


def test_validation_guards(session: Session) -> None:
    """허용 밖 track·max_batches=0·화이트리스트 밖 필드는 fail-loud."""
    with pytest.raises(ValueError):
        create_backfill_job(session, track="B")
    with pytest.raises(ValueError):
        create_backfill_job(session, track="A", max_batches=0)
    job = create_backfill_job(session, track="A")
    with pytest.raises(ValueError):
        update_backfill_job(session, job.id, processed=1)  # 카운터는 record_progress 전용.
    with pytest.raises(ValueError):
        # 종료 전이는 finish_backfill_job 전용(active_track 해제 불변식 우회 차단).
        update_backfill_job(session, job.id, status=FAILED)


def test_migration_backfill_created_at_uses_first_seen(session: Session) -> None:
    """마이그레이션 백필 SQL — 기존 큐 행의 created_at 이 발견 first_seen 으로 보존된다."""
    import importlib.util
    from datetime import datetime, timezone
    from pathlib import Path

    from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow, ReviewQueueRow
    from leadcrawler.storage.review import enqueue_email_review

    mig_path = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions"
        / "a5c9e2d7b4f1_add_backfill_job_and_queue_created_at.py"
    )
    spec = importlib.util.spec_from_file_location("_mig_backfill_created_at", mig_path)
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    seen = datetime(2026, 7, 2, tzinfo=timezone.utc)
    session.add(DiscoveredCompanyRow(
        canonical_key="dom:kr:x.co.kr", name="x", country="KR", first_seen=seen,
    ))
    session.flush()
    session.add(CompanyRow(id="co_x", canonical_key="dom:kr:x.co.kr", name="x", country="KR"))
    session.flush()
    rid = enqueue_email_review(session, "co_x", ["a@x.co.kr"])
    session.flush()

    mig._backfill_created_at(session.connection())
    session.expire_all()
    row = session.get(ReviewQueueRow, rid)
    assert row.created_at.replace(tzinfo=timezone.utc) == seen
