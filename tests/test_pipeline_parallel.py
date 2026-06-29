"""기업 단위 병렬 추출 — 결정성(workers 무관 동일)·진행카운트·취소·과금 스레드안전.

전부 dry_run(네트워크 없음). 병렬 경로는 enrich_workers>1 일 때 활성(dry_run 도 결정적).
"""

from __future__ import annotations

import threading

import leadcrawler.pipeline.run as run_mod
from leadcrawler.config import Settings
from leadcrawler.cost_ledger import CostLedger
from leadcrawler.pipeline import run_pipeline
from leadcrawler.sources.base import DiscoveredCompany, Segment
from leadcrawler.storage.db import init_db


def _many(n: int):
    def _disc(segment, settings, cost_ledger=None, *, sources=None):  # noqa: ARG001
        return [
            DiscoveredCompany(canonical_key=f"dom:c{i}.com", name=f"C{i}", domain=f"c{i}.com")
            for i in range(n)
        ]

    return _disc


def _shape(leads):
    return [(ld.company.canonical_key, ld.email.value if ld.email else None) for ld in leads]


def test_parallel_output_matches_sequential(monkeypatch) -> None:
    seg = [Segment(country="KR", industry="건설")]
    monkeypatch.setattr(run_mod, "discover_segment", _many(30))
    seq = run_pipeline(seg, settings=Settings(dry_run=True, enrich_workers=1))
    par = run_pipeline(seg, settings=Settings(dry_run=True, enrich_workers=4))
    assert len(seq) == 30
    # pool.map 순서보존 + _build_lead 결정성 → 순서·내용 완전 동일.
    assert _shape(seq) == _shape(par)


def test_parallel_progress_counts(monkeypatch) -> None:
    monkeypatch.setattr(run_mod, "discover_segment", _many(20))
    snaps: list[dict] = []
    run_pipeline(
        [Segment(country="KR", industry="건설")],
        settings=Settings(dry_run=True, enrich_workers=4),
        on_progress=lambda p: snaps.append(dict(p)),
    )
    final = snaps[-1]
    assert final["discovered"] == 20
    assert final["enriched"] == 20
    assert final["segments_done"] == 1


def test_parallel_immediate_cancel_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(run_mod, "discover_segment", _many(50))
    leads = run_pipeline(
        [Segment(country="KR", industry="건설")],
        settings=Settings(dry_run=True, enrich_workers=4),
        should_cancel=lambda: True,  # 첫 세그먼트 처리 전 취소 → 빈 결과(크래시 없음).
    )
    assert leads == []


def test_parallel_multi_segment(monkeypatch) -> None:
    # 여러 세그먼트가 각자 배치로 처리되고 누계가 정확.
    monkeypatch.setattr(run_mod, "discover_segment", _many(10))
    leads = run_pipeline(
        [Segment(country="KR", industry="건설"), Segment(country="JP", industry="제조")],
        settings=Settings(dry_run=True, enrich_workers=4),
    )
    # 두 세그먼트가 같은 key 집합을 내므로 도메인 dedup 으로 10건만(두 번째는 전부 스킵).
    assert len(leads) == 10


def test_cost_ledger_record_is_serialized_by_lock(tmp_path) -> None:
    # 락이 record 의 임계구역(_persist_row + 캐시 RMW)을 실제로 직렬화하는지 결정적으로 검증.
    # _persist_row 진입을 프로브로 감싸 동시 진입 수를 잰다 — 락이 있으면 최대 1.
    # (인메모리 append 는 GIL 로 원자라 lost-update 가 비결정적이라 검증 불가 → 직렬화로 검증.
    #  락을 제거하면 max_concurrent 가 1을 넘어 이 테스트가 실패한다.)
    import time

    s = Settings(database_url=f"sqlite:///{tmp_path}/cost.db", dry_run=False)
    init_db(s)
    led = CostLedger(s, persist=True)
    state = {"now": 0, "max": 0}
    probe_lock = threading.Lock()
    real_persist = led._persist_row

    def _probe(ev) -> None:
        with probe_lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.002)  # 임계구역 체류 — 직렬화 안 되면 동시 진입이 잡힌다.
        real_persist(ev)
        with probe_lock:
            state["now"] -= 1

    led._persist_row = _probe  # type: ignore[method-assign]

    def worker() -> None:
        for _ in range(10):
            led.record("hunter")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["max"] == 1  # 락으로 record 가 직렬화됨(동시 진입 없음).
    led.refresh()
    assert led.month_total_krw() == 8 * 10 * led.unit_cost("hunter")  # 누계도 정확.


def test_parallel_skips_failing_company(monkeypatch) -> None:
    # 한 기업이 _build_lead 에서 예외가 나도 배치 전체가 아니라 그 기업만 건너뛴다.
    monkeypatch.setattr(run_mod, "discover_segment", _many(12))
    real = run_mod._build_lead

    def _maybe_raise(dc, **kw):
        if dc.canonical_key == "dom:c5.com":
            raise RuntimeError("boom")
        return real(dc, **kw)

    monkeypatch.setattr(run_mod, "_build_lead", _maybe_raise)
    leads = run_pipeline(
        [Segment(country="KR", industry="건설")],
        settings=Settings(dry_run=True, enrich_workers=4),
    )
    keys = {ld.company.canonical_key for ld in leads}
    assert "dom:c5.com" not in keys  # 실패건만 제외.
    assert len(leads) == 11  # 나머지 11건은 보존(배치 전체 유실 아님).
