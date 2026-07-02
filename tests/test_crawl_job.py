"""crawl_job 저장소 + 백그라운드 러너(동기) 테스트 — 네트워크 0, dry_run."""

from __future__ import annotations

import pytest

from leadcrawler.config import get_settings
from leadcrawler.pipeline import background as bg
from leadcrawler.sources.segments import generate_segments
from leadcrawler.pipeline.background import (
    CrawlBusy,
    CrawlTooLarge,
    is_crawl_running,
    trigger_crawl_job,
)
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


def test_run_crawl_job_completes(settings) -> None:
    # 동기 실행 → done + 카운터 채워짐(dry_run 결정적).
    with session_scope(settings) as db:
        jid = create_crawl_job(
            db, countries="KR", industries="건설", listed="unknown",
            persist=False, segments_total=1, triggered_by="관리자",
        ).id
    segments = generate_segments(["건설"], countries=["KR"], listed=["unknown"])
    bg.run_crawl_job(settings, jid, segments, persist=False)
    with session_scope(settings) as db:
        d = crawl_job_dict(get_crawl_job(db, jid))
        assert d["status"] == "done"
        assert d["discovered"] >= 1 and d["finished_at"] is not None
    assert bg.is_crawl_running() is False  # 가드 해제됨.


def test_run_crawl_job_cancelled(settings) -> None:
    # 시작 전 취소 플래그가 켜져 있으면 cancelled 로 종료.
    with session_scope(settings) as db:
        jid = create_crawl_job(
            db, countries="KR", industries="건설", listed="unknown",
            persist=False, segments_total=1, triggered_by="관리자",
        ).id
        request_cancel(db, jid)
    segments = generate_segments(["건설"], countries=["KR"], listed=["unknown"])
    bg.run_crawl_job(settings, jid, segments, persist=False)
    with session_scope(settings) as db:
        assert get_crawl_job(db, jid).status == "cancelled"


def test_cancel_poller_throttles_and_latches(monkeypatch) -> None:
    # 취소 폴링 throttle + 래치 — 매 호출 DB 안 침(세션 churn 제거), 첫 호출은 즉시 조회.
    calls = {"n": 0}
    flag = {"v": False}

    def _fake_read(sm, jid):  # noqa: ANN001, ARG001
        calls["n"] += 1
        return flag["v"]

    clock = {"t": 1000.0}
    monkeypatch.setattr(bg, "_read_cancel", _fake_read)
    monkeypatch.setattr(bg.time, "monotonic", lambda: clock["t"])

    poll = bg._make_cancel_poller(None, "job", throttle_sec=2.0)
    assert poll() is False and calls["n"] == 1  # 첫 호출 즉시 조회.
    assert poll() is False and calls["n"] == 1  # throttle 내 재호출 — DB 안 침.
    clock["t"] += 2.0
    assert poll() is False and calls["n"] == 2  # 간격 경과 → 재조회.
    flag["v"] = True
    clock["t"] += 2.0
    assert poll() is True and calls["n"] == 3  # 취소 관측.
    assert poll() is True and calls["n"] == 3  # 래치 — 이후 DB 조회 없이 계속 True.


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


def test_trigger_too_large_rejected(settings) -> None:
    # 세그먼트 수가 상한을 넘으면 CrawlTooLarge(가드 점유 없이 거부).
    small = settings.model_copy(update={"crawl_max_segments": 1})
    with pytest.raises(CrawlTooLarge):
        trigger_crawl_job(
            small, countries="KR", industries="건설,반도체", listed="unknown",
            persist=False, triggered_by="x",  # 1국×2업종 = 2 세그먼트 > 1.
        )
    assert is_crawl_running() is False  # 캡 거부는 가드를 건드리지 않는다.


def test_run_crawl_job_continuous_rounds_then_cancel(settings, monkeypatch) -> None:
    # 연속모드 — 취소 관측까지 라운드 반복 + rounds_done 기록, cancelled 로 종료.
    small = settings.model_copy(update={"crawl_loop_pause_sec": 0})
    with session_scope(small) as db:
        jid = create_crawl_job(
            db, countries="KR", industries="건설", listed="unknown",
            persist=False, segments_total=1, triggered_by="관리자", mode="continuous",
        ).id
    calls = {"n": 0}

    def _fake_pipeline(*_a, **_k):
        calls["n"] += 1
        if calls["n"] >= 2:  # 2라운드째가 끝나면 취소 요청 → 라운드 종료 체크에서 관측.
            with session_scope(small) as db:
                request_cancel(db, jid)
        return []

    monkeypatch.setattr(bg, "run_pipeline", _fake_pipeline)
    bg.run_crawl_job(small, jid, [], persist=False, continuous=True)
    with session_scope(small) as db:
        d = crawl_job_dict(get_crawl_job(db, jid))
    assert calls["n"] == 2  # 취소 없었으면 계속 돌았을 것 — 정확히 2라운드에서 멈춤.
    assert d["status"] == "cancelled" and d["rounds_done"] == 2
    assert bg.is_crawl_running() is False  # 가드 해제됨.


def test_run_crawl_job_once_single_round(settings) -> None:
    # 단발(기본) — 1라운드 후 done, rounds_done=1(하위호환: 기존 호출 경로 그대로).
    with session_scope(settings) as db:
        jid = create_crawl_job(
            db, countries="KR", industries="건설", listed="unknown",
            persist=False, segments_total=1, triggered_by="관리자",
        ).id
    segments = generate_segments(["건설"], countries=["KR"], listed=["unknown"])
    bg.run_crawl_job(settings, jid, segments, persist=False)
    with session_scope(settings) as db:
        d = crawl_job_dict(get_crawl_job(db, jid))
    assert d["status"] == "done" and d["rounds_done"] == 1 and d["mode"] == "once"


def test_trigger_continuous_records_mode(settings) -> None:
    # continuous 트리거 — mode='continuous' 로 기록·노출되고 runner 에 플래그 전달.
    seen: dict[str, object] = {}

    def _capture(_s, _jid, _segs, _persist, _target, continuous) -> None:
        seen["continuous"] = continuous

    try:
        info = trigger_crawl_job(
            settings, countries="KR", industries="건설", listed="unknown",
            persist=False, triggered_by="x", runner=_capture, continuous=True,
        )
    finally:
        with bg._guard:  # 동기 러너는 가드를 되돌리지 않으므로 테스트가 직접 해제.
            bg._running = False
    assert info["mode"] == "continuous" and info["rounds_done"] == 0
    assert seen["continuous"] is True
    with session_scope(settings) as db:
        assert get_crawl_job(db, str(info["id"])).mode == "continuous"


def test_trigger_busy_raises(settings, monkeypatch) -> None:
    # 이미 크롤이 도는 중이면 CrawlBusy(라우트에서 409) — 연속잡이 점유 중일 때의 가드.
    monkeypatch.setattr(bg, "_running", True)
    with pytest.raises(CrawlBusy):
        trigger_crawl_job(
            settings, countries="KR", industries="건설", listed="unknown",
            persist=False, triggered_by="x",
        )


def test_pause_cancelled_polls_during_sleep(monkeypatch) -> None:
    # 휴지 중 취소 폴링 — 자는 사이 취소가 켜지면 남은 휴지를 버리고 True.
    flag = {"v": False}
    monkeypatch.setattr(bg, "_read_cancel", lambda *_a: flag["v"])
    clock = {"t": 0.0}
    monkeypatch.setattr(bg.time, "monotonic", lambda: clock["t"])

    def _sleep(sec: float) -> None:
        clock["t"] += sec
        flag["v"] = True  # 자는 사이 취소 요청.

    monkeypatch.setattr(bg.time, "sleep", _sleep)
    assert bg._pause_cancelled(None, "job", pause_sec=10.0) is True
    assert clock["t"] < 10.0  # 휴지를 끝까지 채우지 않고 조기 복귀.


def test_pause_zero_returns_immediately(monkeypatch) -> None:
    # pause 0 — 취소 없으면 자지 않고 즉시 False(다음 라운드로).
    monkeypatch.setattr(bg, "_read_cancel", lambda *_a: False)
    assert bg._pause_cancelled(None, "job", pause_sec=0) is False


def test_trigger_spawn_failure_resets_guard(settings) -> None:
    # 스레드 spawn 이 실패해도 가드가 누수되지 않고, 작업은 failed 로 남는다.
    def _boom(*_args, **_kwargs):
        raise RuntimeError("spawn boom")

    with pytest.raises(RuntimeError, match="spawn boom"):
        trigger_crawl_job(
            settings, countries="KR", industries="건설", listed="unknown",
            persist=False, triggered_by="x", runner=_boom,
        )
    assert is_crawl_running() is False  # 가드 복구됨 — 후속 크롤 가능.
    with session_scope(settings) as db:
        assert latest_crawl_job(db).status == "failed"
