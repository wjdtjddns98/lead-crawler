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


def _many(n: int):
    def _disc(segment, settings, cost_ledger=None):  # noqa: ARG001
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


def test_cost_ledger_record_is_thread_safe() -> None:
    # 8 스레드 동시 record — 누계 lost-update 없이 정확(락 보호).
    led = CostLedger(Settings(dry_run=False), persist=False)

    def worker() -> None:
        for _ in range(100):
            led.record("hunter")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert led.month_total_krw() == 8 * 100 * led.unit_cost("hunter")
