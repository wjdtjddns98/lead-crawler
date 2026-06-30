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
    """discovery_workers=4 산출이 =1 과 dedup·내용·순서까지 완전히 동일(결정성)."""
    seq = run_pipeline(_SEGMENTS, settings=Settings(dry_run=True, discovery_workers=1))
    par = run_pipeline(_SEGMENTS, settings=Settings(dry_run=True, discovery_workers=4))
    assert seq  # 비어있지 않음(실제로 발견·처리됨).
    assert _shape(seq) == _shape(par)


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
