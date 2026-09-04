"""세그먼트 병렬 발견(discovery_workers>1) — 단일 스레드와 동일 산출(결정성·회귀 0).

전부 dry_run(네트워크 없음). 병렬 발견 경로는 discovery_workers>1 일 때만 활성하며,
dry_run 발견은 seen 과 무관·결정적이라 순차(=1)와 dedup·저장 결과가 완전히 동일해야 한다.
"""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.pipeline import run_pipeline
from leadcrawler.sources.base import Segment
from leadcrawler.sources.http import HostRateLimiters
from leadcrawler.sources.registry import build_sources


def _shape(leads):
    return [
        (ld.company.canonical_key, ld.company.is_active, ld.email.value if ld.email else None)
        for ld in leads
    ]


_SEGMENTS = [
    Segment(country="KR", industry="건설"),
    Segment(country="US", industry="금융"),
    Segment(country="영국", industry="제조"),
    Segment(country="PH", industry="전체"),
]


def test_parallel_discovery_matches_sequential() -> None:
    """discovery_workers=4 산출이 =1 과 dedup·내용까지 동일(집합 동치).

    스트리밍은 _consume 순서가 완료순서라 leads 순서는 순차와 달라질 수 있다(결정성 변경) —
    순서에 민감하지 않게 정렬 비교로, 발견·저장된 회사 **집합**이 동일함만 단정한다(제약①②).
    """
    seq = run_pipeline(_SEGMENTS, settings=Settings(dry_run=True, discovery_workers=1))
    par = run_pipeline(_SEGMENTS, settings=Settings(dry_run=True, discovery_workers=4))
    assert seq  # 비어있지 않음(실제로 발견·처리됨).
    assert sorted(_shape(seq)) == sorted(_shape(par))


def test_parallel_discovery_progress_counts_match() -> None:
    """병렬 발견의 최종 진행카운터(발견·보강·저장·세그먼트)가 순차와 동일."""
    seq_snaps: list[dict] = []
    par_snaps: list[dict] = []
    run_pipeline(
        _SEGMENTS,
        settings=Settings(dry_run=True, discovery_workers=1),
        on_progress=lambda p: seq_snaps.append(dict(p)),
    )
    run_pipeline(
        _SEGMENTS,
        settings=Settings(dry_run=True, discovery_workers=4),
        on_progress=lambda p: par_snaps.append(dict(p)),
    )
    assert seq_snaps[-1] == par_snaps[-1]
    assert par_snaps[-1]["segments_done"] == len(_SEGMENTS)


def test_parallel_discovery_immediate_cancel_is_empty() -> None:
    """병렬 경로에서 첫 처리 전 취소 → 빈 결과(크래시 없음)."""
    leads = run_pipeline(
        _SEGMENTS,
        settings=Settings(dry_run=True, discovery_workers=4),
        should_cancel=lambda: True,
    )
    assert leads == []


def test_parallel_discovery_target_saved_lower_bound() -> None:
    """target_saved 는 병렬 발견에서도 하한 보장(saved>=target, 처리 루프에서 평가)."""
    leads = run_pipeline(
        _SEGMENTS,
        settings=Settings(dry_run=True, discovery_workers=4),
        target_saved=3,
    )
    saved = sum(1 for ld in leads if ld.company.is_active)
    assert saved >= 3


def test_build_sources_propagates_rate_limiters() -> None:
    """build_sources(rate_limiters=...) 가 각 소스 내부 페처에 공유 limiter 를 전파한다."""
    hrl = HostRateLimiters(default_rate=5.0)
    sources = build_sources(Settings(dry_run=True), None, rate_limiters=hrl)
    # 모든 소스가 공유 limiter 를 보유(병렬 발견 시 호스트 한도 공유의 전제).
    assert sources and all(getattr(s, "_rate_limiters", "MISSING") is hrl for s in sources)


def test_build_sources_default_rate_limiters_is_none() -> None:
    """기존 호출부(rate_limiters 미지정)는 None — 회귀 0."""
    sources = build_sources(Settings(dry_run=True))
    assert all(getattr(s, "_rate_limiters", None) is None for s in sources)


def test_parallel_discovery_isolates_segment_failure(monkeypatch) -> None:
    """한 세그먼트 발견이 예외를 던져도 그 세그먼트만 빈 결과로 격리되고 런은 완료된다.

    전체 크래시·기적재 유실 방지(순차의 건별 보존과 동등한 blast-radius). 나머지 세그먼트는
    정상 발견·적재되어야 한다.
    """
    from leadcrawler.pipeline import run as run_mod

    real = run_mod.discover_segment

    def flaky(segment, settings, **kw):
        if segment.country == "US":  # US 세그먼트만 발견 실패 시뮬레이션(재시도 소진 등).
            raise RuntimeError("boom")
        return real(segment, settings, **kw)

    monkeypatch.setattr(run_mod, "discover_segment", flaky)
    leads = run_pipeline(_SEGMENTS, settings=Settings(dry_run=True, discovery_workers=4))
    assert leads  # 크래시 없이 완료 + 비어있지 않음.
    countries = {ld.company.country for ld in leads}
    assert "US" not in countries  # 실패한 US 세그먼트는 격리(빈 결과) → 적재 0.
    assert "KR" in countries  # 다른 세그먼트는 정상 적재.


def test_parallel_discovery_no_double_extract_regardless_of_order(monkeypatch) -> None:
    """완료순서가 입력순서와 반대여도 교차세그먼트 중복회사는 1번만 추출된다(제약①).

    스트리밍은 완료순서로 _consume 하므로 뒤 세그먼트가 먼저 완료될 수 있다. 그래도 dedup
    (단일스레드 _consume 의 seen/seen_domains)이 canonical_key/도메인 동치를 한 번만
    통과시켜야 한다(이중추출 = 같은 회사 2번 저장 없음).
    """
    import time

    from leadcrawler.pipeline import run as run_mod
    from leadcrawler.sources.base import DiscoveredCompany

    # 세그먼트 둘이 **같은 회사**(같은 canonical_key/도메인)를 발견하도록 구성.
    shared = DiscoveredCompany(
        canonical_key="dom:shared.example",
        name="공유기업",
        country="KR",
        industry="건설",
        domain="shared.example",
        source="dummy",
    )

    def fake_discover(segment, settings, **kw):
        # 먼저 제출되는 세그먼트(sleep)가 늦게 완료 → 완료순서를 입력순서와 역전시킨다.
        if segment.industry == "slow":
            time.sleep(0.05)
        return [shared]

    monkeypatch.setattr(run_mod, "discover_segment", fake_discover)
    segs = [Segment(country="KR", industry="slow"), Segment(country="KR", industry="fast")]
    leads = run_pipeline(segs, settings=Settings(dry_run=True, discovery_workers=4))
    keys = [ld.company.canonical_key for ld in leads]
    assert keys.count("dom:shared.example") == 1  # 완료순서 무관 — 이중추출 없음.


def test_parallel_discovery_bounded_window(monkeypatch) -> None:
    """in-flight(제출−소비) 세그먼트 수가 window(=discovery_workers*2) 이하로 유지된다(EP1).

    옛 배리어(전량 선제출)였다면 in-flight 가 세그먼트 총수(=12)까지 치솟아 이 상한을 깬다 —
    즉 이 테스트는 미소비 발견결과의 무한누적(메모리 스파이크)을 회귀로 잡는다.
    """
    from leadcrawler.pipeline import run as run_mod

    peak = 0

    def observe(n: int) -> None:
        nonlocal peak
        peak = max(peak, n)

    monkeypatch.setattr(run_mod, "_WINDOW_OBSERVER", observe)
    workers = 2
    industries = ["건설", "금융", "제조", "전체"]
    segs = [Segment(country="KR", industry=industries[i % 4]) for i in range(12)]
    run_pipeline(segs, settings=Settings(dry_run=True, discovery_workers=workers))
    assert 0 < peak <= workers * 2  # window 상한 준수(배리어면 peak==12 로 실패).


def test_stage_timing_observer_fires() -> None:
    """런 종료 시 스테이지 병목 요약이 관측 훅으로 1회 전달된다(발견/추출 worker-초·straggler).

    chunking 후속의 게이트 데이터 — 요약 dict 에 세그먼트 수·회사 수·병목 분류가 담긴다.
    """
    from leadcrawler.pipeline import run as run_mod

    seen: list[dict] = []
    run_mod._TIMING_OBSERVER = seen.append  # type: ignore[attr-defined]
    try:
        run_pipeline(_SEGMENTS, settings=Settings(dry_run=True, discovery_workers=4))
    finally:
        run_mod._TIMING_OBSERVER = None  # 전역 훅 원복(다른 테스트 오염 방지).

    assert len(seen) == 1  # 런당 정확히 1회.
    s = seen[0]
    assert s["segments"] == len(_SEGMENTS)
    assert s["companies"] > 0  # dry_run 더미가 발견됨.
    assert s["bottleneck"] in {"discovery", "enrich"}
    assert s["discovery_worker_sec"] >= 0 and s["enrich_worker_sec"] >= 0
    assert 0.0 <= s["straggler_share"] <= 1.0


def test_stage_timing_identifies_straggler(monkeypatch) -> None:
    """한 세그먼트 발견이 유독 오래 걸리면 straggler 로 지목된다(chunking 대상 식별).

    발견 시간이 한 세그먼트에 몰리면(share 高) sub-segment chunking 이 값어치라는 신호.
    """
    import time

    from leadcrawler.pipeline import run as run_mod

    real = run_mod.discover_segment

    def slow_one(segment, settings, **kw):
        if segment.industry == "건설":  # 이 세그먼트만 느리게(straggler 유발).
            time.sleep(0.05)
        return real(segment, settings, **kw)

    monkeypatch.setattr(run_mod, "discover_segment", slow_one)
    seen: list[dict] = []
    monkeypatch.setattr(run_mod, "_TIMING_OBSERVER", seen.append)
    run_pipeline(_SEGMENTS, settings=Settings(dry_run=True, discovery_workers=4))

    assert len(seen) == 1
    assert seen[0]["straggler_seg"].startswith("KR/건설")  # 느린 세그먼트가 straggler.
    assert seen[0]["straggler_share"] > 0.5  # 발견 시간의 과반이 이 세그먼트에 몰림.


def test_interleave_by_country_round_robin() -> None:
    """국가 라운드로빈 재배열 — 같은 국가(=같은 등록처 호스트)가 연속으로 몰리지 않는다."""
    from leadcrawler.pipeline.run import _interleave_by_country

    segs = [
        Segment(country="KR", industry="반도체"),
        Segment(country="KR", industry="자동차"),
        Segment(country="KR", industry="화학"),
        Segment(country="US", industry="반도체"),
        Segment(country="US", industry="자동차"),
        Segment(country="GB", industry="반도체"),
    ]
    order = _interleave_by_country(segs)
    # 전 인덱스가 정확히 1번씩(순열) — 누락·중복 없이 전부 제출된다.
    assert sorted(order) == list(range(len(segs)))
    # KR,US,GB,KR,US,KR 순 — 첫 웨이브가 서로 다른 국가로 분산된다.
    assert [segs[i].country for i in order] == ["KR", "US", "GB", "KR", "US", "KR"]
    # 단일 국가면 원 순서 그대로(무해).
    only_kr = segs[:3]
    assert _interleave_by_country(only_kr) == [0, 1, 2]
    assert _interleave_by_country([]) == []
