"""dry_run 파이프라인 + 중복제거 테스트."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.models import ValidationStatus
from leadcrawler.pipeline import run_pipeline
from leadcrawler.sources.base import DiscoveredCompany, Segment


def test_dry_run_pipeline_produces_leads() -> None:
    leads = run_pipeline([Segment(country="KR", industry="건설")])
    assert leads
    lead = leads[0]
    assert lead.company.is_active is True
    assert lead.email is not None and lead.email.value.startswith("ir@")
    assert lead.form is not None
    assert lead.email_validation.status is ValidationStatus.VALID


def test_target_saved_stops_early(monkeypatch) -> None:
    """target_saved 도달 시 남은 세그먼트를 돌지 않고 조기 종료('정해진 양만큼 뽑고 멈춤')."""
    import leadcrawler.pipeline.run as run_mod

    def _fake_discover(segment, settings, cost_ledger=None, *, sources=None):  # noqa: ARG001
        # 세그먼트마다 활성 더미 5개(도메인 유일 → dedup 스킵 안 됨).
        return [
            DiscoveredCompany(
                canonical_key=f"dom:{segment.industry}{i}.com",
                name=f"C{i}", domain=f"{segment.industry}{i}.com",
            )
            for i in range(5)
        ]

    monkeypatch.setattr(run_mod, "discover_segment", _fake_discover)
    segs = [Segment(country="KR", industry=f"ind{j}") for j in range(10)]  # 최대 50 가능
    leads = run_pipeline(
        segs, settings=Settings(dry_run=True, enrich_workers=1), target_saved=7
    )
    saved = [ld for ld in leads if ld.company.is_active]
    assert len(saved) >= 7  # 목표 도달
    assert len(saved) < 50  # 전부 안 돌고 조기 종료(과수확 제한)


def test_no_target_processes_all_segments(monkeypatch) -> None:
    """target_saved=None(기본)이면 주어진 세그먼트를 전부 소진(continuous, 회귀 0)."""
    import leadcrawler.pipeline.run as run_mod

    def _fake_discover(segment, settings, cost_ledger=None, *, sources=None):  # noqa: ARG001
        return [
            DiscoveredCompany(
                canonical_key=f"dom:{segment.industry}{i}.com",
                name=f"C{i}", domain=f"{segment.industry}{i}.com",
            )
            for i in range(5)
        ]

    monkeypatch.setattr(run_mod, "discover_segment", _fake_discover)
    segs = [Segment(country="KR", industry=f"ind{j}") for j in range(4)]  # 4×5=20
    leads = run_pipeline(segs, settings=Settings(dry_run=True, enrich_workers=1))
    assert len([ld for ld in leads if ld.company.is_active]) == 20  # 전부 처리.


def test_seen_keys_dedup() -> None:
    seg = Segment(country="KR", industry="건설")
    seen: set[str] = set()
    first = run_pipeline([seg], seen=seen)
    assert first
    # 같은 세그먼트를 같은 seen 으로 재실행하면 전부 스킵.
    second = run_pipeline([seg], seen=seen)
    assert second == []


def test_cross_segment_domain_dedup(monkeypatch) -> None:
    # 같은 도메인을 서로 다른 세그먼트가 다른 key(reg:/dom:)로 잡아도 런 전체에서 1회만 추출.
    from leadcrawler.sources.base import DiscoveredCompany

    def _fake_discover(segment, settings, cost_ledger=None, *, sources=None):  # noqa: ARG001
        if segment.industry == "건설":
            return [DiscoveredCompany(
                canonical_key="reg:dart:001", name="삼성", domain="samsung.com",
                registry="dart", registry_id="001", source="dart",
            )]
        return [DiscoveredCompany(
            canonical_key="dom:samsung.com", name="삼성전자",
            domain="https://www.samsung.com", source="search",
        )]

    import leadcrawler.pipeline.run as run_mod

    monkeypatch.setattr(run_mod, "discover_segment", _fake_discover)
    leads = run_pipeline([
        Segment(country="KR", industry="건설"),
        Segment(country="KR", industry="제조"),
    ])
    # 두 번째 세그먼트의 dom: 후보는 도메인 동치로 스킵 → 1건만.
    assert len(leads) == 1
    assert leads[0].company.canonical_key == "reg:dart:001"


def test_on_progress_reports_counters() -> None:
    # on_progress 가 단계마다 카운터 dict 를 통지하고, 최종값이 결과와 일치한다.
    events: list[dict[str, int]] = []
    leads = run_pipeline(
        [Segment(country="KR", industry="건설")],
        on_progress=events.append,
    )
    assert events  # 최소 1회(초기 + 기업별 + 세그먼트 완료) 통지.
    first = events[0]
    assert first["segments_total"] == 1  # 초기 통지에 총 세그먼트 수.
    last = events[-1]
    assert last["segments_done"] == 1  # 세그먼트 완료까지 진행.
    assert last["discovered"] == len(leads)
    assert last["enriched"] == len(leads)
    assert last["saved"] == sum(1 for ld in leads if ld.company.is_active)


def test_should_cancel_stops_before_processing() -> None:
    # should_cancel 이 처음부터 True → 어떤 기업도 처리하지 않고 빈 결과로 중단.
    leads = run_pipeline(
        [Segment(country="KR", industry="건설")],
        should_cancel=lambda: True,
    )
    assert leads == []


def test_should_cancel_midway_preserves_processed(monkeypatch) -> None:
    # 첫 기업 처리 후 취소 신호 → 처리된 분은 보존, 이후 기업은 중단.
    from leadcrawler.sources.base import DiscoveredCompany

    def _fake_discover(segment, settings, cost_ledger=None, *, sources=None):  # noqa: ARG001
        return [
            DiscoveredCompany(
                canonical_key="dom:a.com", name="A", domain="a.com", source="search"
            ),
            DiscoveredCompany(
                canonical_key="dom:b.com", name="B", domain="b.com", source="search"
            ),
        ]

    import leadcrawler.pipeline.run as run_mod

    monkeypatch.setattr(run_mod, "discover_segment", _fake_discover)
    calls = {"n": 0}

    def _cancel() -> bool:
        # 호출 순서: ①세그먼트 시작 ②A 처리 직전 ③B 처리 직전. ③에서만 취소.
        calls["n"] += 1
        return calls["n"] > 2

    leads = run_pipeline([Segment(country="KR", industry="건설")], should_cancel=_cancel)
    assert len(leads) == 1
    assert leads[0].company.canonical_key == "dom:a.com"
