"""crawl_job 저장소 테스트 — 네트워크 0, dry_run.

크롤실행(즉시 크롤) 백그라운드 러너·워치독은 2026-09-01 제거됨(세그먼트 작업 큐로
일원화). crawl_job 테이블 자체는 스케줄러(`_continuous_crawl_running`)·이력이
의존하므로 저장소 레이어 테스트만 남긴다.
"""

from __future__ import annotations

import pytest

from leadcrawler.config import get_settings
from leadcrawler.storage.crawl_job import (
    active_crawl_job,
    create_crawl_job,
    crawl_job_dict,
    fail_running_jobs,
    get_crawl_job,
    is_cancel_requested,
    latest_crawl_job,
    request_cancel,
    update_crawl_job,
)
from leadcrawler.storage.db import init_db, session_scope


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/cj.db")
    get_settings.cache_clear()
    s = get_settings()
    init_db(s)
    return s


def test_create_and_fetch(settings) -> None:
    with session_scope(settings) as db:
        row = create_crawl_job(
            db, countries="KR", industries="건설", listed="unknown",
            persist=True, segments_total=3, triggered_by="관리자",
        )
        jid = row.id
        assert jid.startswith("cj_")
    with session_scope(settings) as db:
        got = get_crawl_job(db, jid)
        assert got is not None and got.status == "running"
        assert got.segments_total == 3 and got.triggered_by == "관리자"
        assert active_crawl_job(db).id == jid
        assert latest_crawl_job(db).id == jid


def test_update_counters_and_dict(settings) -> None:
    with session_scope(settings) as db:
        jid = create_crawl_job(
            db, countries="", industries="건설", listed="unknown",
            persist=False, segments_total=1, triggered_by=None,
        ).id
    with session_scope(settings) as db:
        update_crawl_job(db, jid, discovered=5, enriched=4, saved=3, segments_done=1)
    with session_scope(settings) as db:
        d = crawl_job_dict(get_crawl_job(db, jid))
        assert d["discovered"] == 5 and d["enriched"] == 4 and d["saved"] == 3
        assert d["segments_done"] == 1
        assert d["started_at"] is not None  # ISO 문자열로 평탄화.


def test_update_rejects_unknown_field(settings) -> None:
    with session_scope(settings) as db:
        jid = create_crawl_job(
            db, countries="", industries="건설", listed="unknown",
            persist=False, segments_total=1, triggered_by=None,
        ).id
    with session_scope(settings) as db:
        with pytest.raises(ValueError):
            update_crawl_job(db, jid, triggered_by="해커")  # 화이트리스트 밖.


def test_request_cancel_sets_flag(settings) -> None:
    with session_scope(settings) as db:
        jid = create_crawl_job(
            db, countries="", industries="건설", listed="unknown",
            persist=False, segments_total=1, triggered_by=None,
        ).id
    with session_scope(settings) as db:
        assert is_cancel_requested(db, jid) is False
        request_cancel(db, jid)
    with session_scope(settings) as db:
        assert is_cancel_requested(db, jid) is True


def test_request_cancel_noop_on_terminal(settings) -> None:
    # 이미 done 인 작업엔 취소 플래그를 켜지 않는다(멱등·무의미).
    with session_scope(settings) as db:
        jid = create_crawl_job(
            db, countries="", industries="건설", listed="unknown",
            persist=False, segments_total=1, triggered_by=None,
        ).id
        update_crawl_job(db, jid, status="done")
    with session_scope(settings) as db:
        request_cancel(db, jid)
        assert is_cancel_requested(db, jid) is False


def test_fail_running_jobs_bulk(settings) -> None:
    # 남은 running 행을 전부 failed 로 일괄 정리(다중 잔재 누적 방지).
    with session_scope(settings) as db:
        for _ in range(3):
            create_crawl_job(
                db, countries="KR", industries="건설", listed="unknown",
                persist=False, segments_total=1, triggered_by="x",
            )
    with session_scope(settings) as db:
        n = fail_running_jobs(db, "재시작 정리")
        assert n == 3
    with session_scope(settings) as db:
        assert active_crawl_job(db) is None  # running 0건.
