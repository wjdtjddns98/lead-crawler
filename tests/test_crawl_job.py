"""crawl_job 저장소 + 백그라운드 러너(동기) 테스트 — 네트워크 0, dry_run."""

from __future__ import annotations

import threading

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


# --- 워치독 --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_watchdog_globals():
    """전역 _running·_watchdog_started 를 테스트마다 스냅샷·복원(순서 의존·누수 차단)."""
    run0, started0 = bg._running, bg._watchdog_started
    bg._running, bg._watchdog_started = False, False
    yield
    bg._running, bg._watchdog_started = run0, started0


_DEAD = lambda _jid: False  # 스레드 부재(소멸) 시뮬.  # noqa: E731
_ALIVE = lambda _jid: True  # 스레드 생존 시뮬.  # noqa: E731


def _make_continuous(settings, mode: str = "continuous", **snap) -> str:
    with session_scope(settings) as db:
        row = create_crawl_job(
            db, countries="KR", industries="건설", listed="unknown",
            persist=True, segments_total=1, triggered_by="x", mode=mode, **snap,
        )
        return row.id


def test_watchdog_reaps_dead_thread_and_restarts_continuous(settings, monkeypatch) -> None:
    # 실행 스레드 소멸(부재) → failed 정리 + 같은 파라미터로 재기동(runner 주입).
    monkeypatch.setattr(settings, "crawl_watchdog_grace_misses", 1)  # 즉시 reap(grace 없음).
    dead = _make_continuous(settings)
    restarts: list[bool] = []

    def _capture(_s, _jid, _segs, _persist, _target, continuous) -> None:
        restarts.append(continuous)

    assert bg._watchdog_tick(
        settings, runner=_capture, misses={}, alive_check=_DEAD
    ) is True
    assert restarts == [True]  # continuous 재기동됨.
    with session_scope(settings) as db:
        assert get_crawl_job(db, dead).status == "failed"
        alive = active_crawl_job(db)
        assert alive is not None and alive.id != dead and alive.triggered_by == "watchdog"
    assert is_crawl_running() is True  # 재기동이 가드를 다시 점유.


def test_watchdog_ignores_live_thread_during_slow_discovery(settings) -> None:
    # 핵심 오탐 방지: 스레드가 살아있으면(긴 병렬 발견 무-emit 윈도 포함) 절대 reap 안 함.
    jid = _make_continuous(settings)

    def _capture(*_a, **_k) -> None:
        raise AssertionError("살아있는 스레드를 reap 하면 안 됨")

    assert bg._watchdog_tick(
        settings, runner=_capture, misses={}, alive_check=_ALIVE
    ) is False
    with session_scope(settings) as db:
        assert get_crawl_job(db, jid).status == "running"  # 그대로.


def test_watchdog_grace_defers_first_miss(settings, monkeypatch) -> None:
    # grace_misses=2: 첫 부재는 유예(create/start 마이크로갭 오탐 방지), 둘째에 reap.
    monkeypatch.setattr(settings, "crawl_watchdog_grace_misses", 2)
    jid = _make_continuous(settings)
    misses: dict[str, int] = {}
    noop = lambda *a: None  # noqa: E731
    # 1회차: 유예(reap 안 함).
    assert bg._watchdog_tick(settings, misses=misses, alive_check=_DEAD, runner=noop) is False
    with session_scope(settings) as db:
        assert get_crawl_job(db, jid).status == "running"
    # 2회차: grace 도달 → reap.
    assert bg._watchdog_tick(settings, misses=misses, alive_check=_DEAD, runner=noop) is True
    with session_scope(settings) as db:
        assert get_crawl_job(db, jid).status == "failed"


def test_watchdog_reaps_once_mode_without_restart(settings, monkeypatch) -> None:
    # once 모드 소멸분은 정리만 하고 재기동하지 않는다(continuous 아님).
    monkeypatch.setattr(settings, "crawl_watchdog_grace_misses", 1)
    dead = _make_continuous(settings, mode="once")

    def _capture(*_a, **_k) -> None:
        raise AssertionError("once 는 재기동 안 함")

    assert bg._watchdog_tick(
        settings, runner=_capture, misses={}, alive_check=_DEAD
    ) is True
    with session_scope(settings) as db:
        assert get_crawl_job(db, dead).status == "failed"
        assert active_crawl_job(db) is None  # 재기동 없음 → running 0.
    assert is_crawl_running() is False  # 좀비 가드 강제 해제.


def test_watchdog_noop_when_no_active_job(settings) -> None:
    assert bg._watchdog_tick(settings, misses={}, alive_check=_DEAD) is False


def test_start_watchdog_noop_on_dry_run_and_disabled(settings, monkeypatch) -> None:
    # dry_run 기본 True → no-op.
    assert bg.start_watchdog(settings) is False
    # 비활성(enabled=False) 도 no-op.
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "crawl_watchdog_enabled", False)
    assert bg.start_watchdog(settings) is False
    assert bg._watchdog_started is False


def test_discovery_only_disables_email_escalation(settings) -> None:
    # discovery_only=True → 비싼 escalation(헤드리스·OCR·이메일API·Vision) 전부 꺼진 settings 로
    # 파이프라인 실행(static 만 인라인, 무이메일은 별도 채우기 패스가 담당).
    on = settings.model_copy(update={
        "enrich_headless": True, "enrich_ocr": True,
        "enrich_email_api": True, "enrich_vision": True,
    })
    seen: dict[str, object] = {}

    def _capture(s, _jid, _segs, _persist, _target, _continuous) -> None:
        seen["s"] = s

    try:
        trigger_crawl_job(
            on, countries="KR", industries="건설", listed="unknown",
            persist=False, triggered_by="x", runner=_capture, discovery_only=True,
        )
    finally:
        with bg._guard:
            bg._running = False
    s = seen["s"]
    assert s.enrich_headless is False and s.enrich_ocr is False
    assert s.enrich_email_api is False and s.enrich_vision is False


def test_discovery_only_default_keeps_escalation(settings) -> None:
    # 기본(discovery_only=False) → 입력 settings 의 escalation 플래그를 그대로 둔다(회귀 0).
    on = settings.model_copy(update={"enrich_headless": True, "enrich_ocr": True})
    seen: dict[str, object] = {}

    def _capture(s, _jid, _segs, _persist, _target, _continuous) -> None:
        seen["s"] = s

    try:
        trigger_crawl_job(
            on, countries="KR", industries="건설", listed="unknown",
            persist=False, triggered_by="x", runner=_capture,
        )
    finally:
        with bg._guard:
            bg._running = False
    assert seen["s"].enrich_headless is True and seen["s"].enrich_ocr is True


def test_discovery_only_spawns_email_consumer(settings, monkeypatch) -> None:
    # discovery_only 크롤(실 스폰경로) → 발견 스레드 + 이메일 consumer 스레드 둘 다 스폰.
    live = settings.model_copy(update={"dry_run": False})
    calls = {"crawl": 0, "consumer": 0}
    monkeypatch.setattr(bg, "_spawn_thread", lambda *a, **k: calls.__setitem__("crawl", calls["crawl"] + 1))
    monkeypatch.setattr(bg, "_spawn_consumer_thread", lambda *a, **k: calls.__setitem__("consumer", calls["consumer"] + 1))
    try:
        trigger_crawl_job(
            live, countries="KR", industries="건설", listed="unknown",
            persist=False, triggered_by="x", discovery_only=True,
        )
    finally:
        with bg._guard:
            bg._running = False
    assert calls["crawl"] == 1 and calls["consumer"] == 1


def test_normal_crawl_does_not_spawn_consumer(settings, monkeypatch) -> None:
    # discovery_only=False(인라인) → consumer 미스폰(인라인이 이미 이메일 채움).
    live = settings.model_copy(update={"dry_run": False})
    calls = {"consumer": 0}
    monkeypatch.setattr(bg, "_spawn_thread", lambda *a, **k: None)
    monkeypatch.setattr(bg, "_spawn_consumer_thread", lambda *a, **k: calls.__setitem__("consumer", calls["consumer"] + 1))
    try:
        trigger_crawl_job(
            live, countries="KR", industries="건설", listed="unknown",
            persist=False, triggered_by="x", discovery_only=False,
        )
    finally:
        with bg._guard:
            bg._running = False
    assert calls["consumer"] == 0


def test_continuous_rounds_accumulate_counters(settings, monkeypatch) -> None:
    # 연속모드 카운터 누계 — run_pipeline 이 라운드마다 0에서 시작한 counts 를 emit 해도
    # crawl_job 에는 라운드 합(마지막 라운드만 아님)으로 기록돼야 한다.
    small = settings.model_copy(update={"crawl_loop_pause_sec": 0})
    with session_scope(small) as db:
        jid = create_crawl_job(
            db, countries="KR", industries="건설", listed="unknown",
            persist=False, segments_total=1, triggered_by="x", mode="continuous",
        ).id
    rounds = [
        {"segments_total": 1, "segments_done": 1, "discovered": 5, "enriched": 4, "saved": 3},
        {"segments_total": 1, "segments_done": 1, "discovered": 2, "enriched": 2, "saved": 1},
    ]
    calls = {"n": 0}

    def _fake_pipeline(*_a, **k):
        i = calls["n"]
        calls["n"] += 1
        k["on_progress"](dict(rounds[i]))  # 그 라운드분(0에서 시작) emit.
        if calls["n"] >= 2:
            with session_scope(small) as db:
                request_cancel(db, jid)
        return []

    monkeypatch.setattr(bg, "run_pipeline", _fake_pipeline)
    bg.run_crawl_job(small, jid, [], persist=False, continuous=True)
    with session_scope(small) as db:
        d = crawl_job_dict(get_crawl_job(db, jid))
    # 누계: discovered 5+2=7, enriched 4+2=6, saved 3+1=4 (마지막 라운드만이면 2/2/1).
    assert d["discovered"] == 7 and d["enriched"] == 6 and d["saved"] == 4
    assert d["rounds_done"] == 2


def test_crawl_never_spawns_domain_backfill_companion(settings, monkeypatch) -> None:
    # 크롤 실행 = 딱 크롤만(운영 지시 2026-07-27) — resolve_domains opt-in 이어도 도메인 백필
    # companion 은 병행 스폰하지 않는다(백필은 별도 CLI backfill-resolve-domains 로 명시 실행).
    assert not hasattr(bg, "_spawn_domain_backfill_thread")  # 이름 재도입 가드.
    live = settings.model_copy(update={"dry_run": False, "resolve_domains": True})
    calls = {"crawl": 0}
    spawned: list[str] = []  # bg 모듈이 띄우는 모든 스레드 스파이 — 다른 이름 재도입도 잡는다.

    class _SpyThread(threading.Thread):
        def start(self) -> None:  # 실제 스폰 없이 기록만.
            spawned.append(self.name)

    monkeypatch.setattr(bg.threading, "Thread", _SpyThread)
    monkeypatch.setattr(bg, "_spawn_thread", lambda *a, **k: calls.__setitem__("crawl", calls["crawl"] + 1))
    try:
        trigger_crawl_job(
            live, countries="KR", industries="건설", listed="unknown",
            persist=True, triggered_by="x", discovery_only=False,
        )
    finally:
        with bg._guard:
            bg._running = False
    # 발견 스레드(monkeypatch 경유) 1회 외에 bg 가 어떤 부수 스레드도 만들지 않아야 한다.
    assert calls["crawl"] == 1 and spawned == []


def test_dry_run_discovery_only_does_not_spawn_email_consumer(settings, monkeypatch) -> None:
    # dry_run(기본 fixture) → discovery_only 여도 이메일 consumer 미스폰(결정적 유지, §2 계약).
    calls = {"consumer": 0}
    monkeypatch.setattr(bg, "_spawn_thread", lambda *a, **k: None)
    monkeypatch.setattr(
        bg, "_spawn_consumer_thread",
        lambda *a, **k: calls.__setitem__("consumer", calls["consumer"] + 1),
    )
    try:
        trigger_crawl_job(
            settings, countries="KR", industries="건설", listed="unknown",
            persist=True, triggered_by="x", discovery_only=True,
        )
    finally:
        with bg._guard:
            bg._running = False
    assert calls["consumer"] == 0


def test_watchdog_restart_restores_option_snapshot(settings, monkeypatch) -> None:
    """재기동이 target_count·regions·discovery_only 스냅샷을 복원한다(전수리뷰 HIGH).

    미복원 구동작: 지역한정→전국 확대·상한 소멸·discovery_only→풀 enrich 로
    범위·비용이 원 요청과 달라졌다.
    """
    monkeypatch.setattr(settings, "crawl_watchdog_grace_misses", 1)
    dead = _make_continuous(
        settings, target_count=7, regions="서울특별시", discovery_only=True
    )
    captured: list[int] = []

    def _capture(_s, _jid, _segs, _persist, target, _continuous) -> None:
        captured.append(target)

    assert bg._watchdog_tick(settings, runner=_capture, misses={}, alive_check=_DEAD) is True
    assert captured == [7]  # target_count 상한 복원.
    with session_scope(settings) as db:
        alive = active_crawl_job(db)
        assert alive is not None and alive.id != dead
        # 새 잡 행에도 스냅샷이 그대로 이어진다(다음 재기동 체인 보존).
        assert alive.target_count == 7
        assert alive.regions == "서울특별시"
        assert alive.discovery_only is True


def test_crawl_job_info_exposes_option_snapshot(settings) -> None:
    """API 응답모델이 스냅샷 3필드를 실제로 노출한다(교차리뷰 HIGH — pydantic extra 드롭).

    미선언이면 crawl_job_dict 가 값을 실어도 CrawlJobInfo(**dict) 가 조용히 버린다.
    """
    from leadcrawler.api.schemas import CrawlJobInfo
    from leadcrawler.storage.crawl_job import crawl_job_dict, get_crawl_job

    jid = _make_continuous(settings, target_count=5, regions="부산광역시", discovery_only=True)
    with session_scope(settings) as db:
        info = CrawlJobInfo(**crawl_job_dict(get_crawl_job(db, jid)))
    dumped = info.model_dump()
    assert dumped["target_count"] == 5
    assert dumped["regions"] == "부산광역시"
    assert dumped["discovery_only"] is True
