"""세그먼트 내부 소스병렬(discovery_source_workers) — 정확성·동시성·게이팅 보존 검증.

병렬은 비검색(무료) 소스의 discover 만 스레드풀로 돌리고, 병합·dedup·검색 게이팅은
main 스레드에서 src_list 순서대로 하므로 결과는 순차와 결정적으로 동일해야 한다.
"""

from __future__ import annotations

import threading

import pytest

from leadcrawler.config import Settings
from leadcrawler.sources.base import DiscoveredCompany, Segment
from leadcrawler.sources.registry import discover_segment
from leadcrawler.sources.search import SearchSource

SEG = Segment(country="KR", industry="제조")


def _settings(**over: object) -> Settings:
    # KR 다소스 발견을 전제로 한 병렬 메커니즘 테스트라 NPS 단독 정책은 끈다
    # (정책 자체는 test_sources 의 kr_nps_only_policy 테스트가 검증).
    over.setdefault("kr_discovery_nps_only", False)
    return Settings(dry_run=True, **over)


def _dc(key: str, domain: str | None = None, source: str = "stub") -> DiscoveredCompany:
    return DiscoveredCompany(canonical_key=key, name=key, domain=domain, source=source)


class _Stub:
    """무료(비검색) 소스 스텁 — 결과 고정 + 실행 스레드 기록."""

    def __init__(self, name: str, results: list[DiscoveredCompany]) -> None:
        self.name = name
        self._results = results
        self.threads: list[str] = []

    def applies_to(self, segment: Segment) -> bool:  # noqa: ARG002
        return True

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:  # noqa: ARG002
        self.threads.append(threading.current_thread().name)
        return list(self._results)


class _SpySearch(SearchSource):
    """검색 소스 스파이 — 호출수·주입 seen 기록."""

    def __init__(self, settings: Settings, results: list[DiscoveredCompany]) -> None:
        super().__init__(settings)
        self.calls = 0
        self.seen_arg: set | None = None
        self._results = results

    def applies_to(self, segment: Segment) -> bool:  # noqa: ARG002
        return True

    def discover(self, segment: Segment, *, seen: set | None = None) -> list:  # noqa: ARG002
        self.calls += 1
        self.seen_arg = seen
        return list(self._results)


def _three_sources() -> list[_Stub]:
    """key 중복(a1)·도메인 동치(dup.com)를 포함한 3개 소스 — 첫 등장 우선 검증용."""
    return [
        _Stub("s1", [_dc("reg:a1", "a1.com"), _dc("reg:a2", "dup.com")]),
        _Stub("s2", [_dc("reg:a1", "a1.com"), _dc("reg:b1", "b1.com")]),  # a1 = key 중복.
        _Stub("s3", [_dc("reg:c1", "dup.com"), _dc("reg:c2", None)]),  # c1 = 도메인 동치.
    ]


def test_parallel_result_equals_sequential() -> None:
    """workers>1 결과(내용·순서)가 순차(=1)와 동일 — 첫 등장 우선 dedup 보존."""
    seq = discover_segment(SEG, _settings(), sources=_three_sources())
    par = discover_segment(
        SEG, _settings(discovery_source_workers=4), sources=_three_sources()
    )
    assert [r.canonical_key for r in par] == [r.canonical_key for r in seq]
    assert [r.canonical_key for r in par] == ["reg:a1", "reg:a2", "reg:b1", "reg:c2"]


def test_parallel_runs_sources_concurrently() -> None:
    """두 소스가 서로를 기다리는 배리어를 통과 — 실제 동시 실행이어야만 성공한다."""
    barrier = threading.Barrier(2, timeout=5)

    class _Blocking(_Stub):
        def discover(self, segment: Segment) -> list[DiscoveredCompany]:  # noqa: ARG002
            barrier.wait()  # 순차 실행이면 BrokenBarrierError(timeout).
            return list(self._results)

    srcs = [_Blocking("b1", [_dc("reg:x1")]), _Blocking("b2", [_dc("reg:x2")])]
    rows = discover_segment(SEG, _settings(discovery_source_workers=2), sources=srcs)
    assert {r.canonical_key for r in rows} == {"reg:x1", "reg:x2"}


def test_parallel_preserves_search_gating_and_injection() -> None:
    """병렬이어도 검색은 병합 후 순차 — 무료 도메인 주입·free_new 스킵(②) 보존."""
    free = _Stub("free", [_dc("reg:a1", "a1.com")])
    spy = _SpySearch(_settings(), results=[_dc("dom:new.com", "new.com", source="search")])
    rows = discover_segment(
        SEG,
        _settings(discovery_source_workers=4),
        sources=[free, spy],
        seen_domains={"db-seed.com"},
    )
    assert spy.seen_arg == {"db-seed.com", "a1.com"}
    assert {r.domain for r in rows} == {"a1.com", "new.com"}

    skip_spy = _SpySearch(_settings(), results=[])
    rows = discover_segment(
        SEG,
        _settings(discovery_source_workers=4, search_skip_if_free_ge=1),
        sources=[_Stub("free", [_dc("reg:a1", "a1.com")]), skip_spy],
    )
    assert skip_spy.calls == 0  # 무료 1건 커버 → 유료 검색 스킵.


def test_parallel_source_error_fails_segment() -> None:
    """소스 예외는 부분 결과로 삼키지 않고 순차와 동일하게 전파된다."""

    class _Boom(_Stub):
        def discover(self, segment: Segment) -> list[DiscoveredCompany]:  # noqa: ARG002
            raise RuntimeError("boom")

    srcs = [_Stub("ok", [_dc("reg:a1")]), _Boom("boom", [])]
    with pytest.raises(RuntimeError, match="boom"):
        discover_segment(SEG, _settings(discovery_source_workers=2), sources=srcs)


def test_free_source_after_search_not_injected_and_order_preserved() -> None:
    """검색 *뒤* 무료 소스(AI 디렉토리 위치)는 주입·free_new 에 미포함 + 순차선 호출 순서 원형."""
    calls: list[str] = []

    class _Traced(_Stub):
        def discover(self, segment: Segment) -> list[DiscoveredCompany]:
            calls.append(self.name)
            return super().discover(segment)

    class _TracedSearch(_SpySearch):
        def discover(self, segment: Segment, *, seen: set | None = None) -> list:
            calls.append("search")
            return super().discover(segment, seen=seen)

    def _srcs() -> list:
        return [
            _Traced("before", [_dc("reg:a1", "a1.com")]),
            _TracedSearch(_settings(), results=[]),
            _Traced("after", [_dc("reg:z1", "z1.com")]),
        ]

    for workers in (1, 4):
        calls.clear()
        srcs = _srcs()
        rows = discover_segment(
            SEG, _settings(discovery_source_workers=workers), sources=srcs, seen_domains=set()
        )
        # 검색 뒤 무료 소스의 도메인은 주입에 없다(주입 = 검색 이전까지의 병합 상태).
        assert srcs[1].seen_arg == {"a1.com"}, f"workers={workers}"
        assert {r.canonical_key for r in rows} == {"reg:a1", "reg:z1"}
        if workers == 1:
            # 순차는 호출 순서까지 원형(검색 뒤 유료 소스의 예산게이트 평가 순서 보존).
            assert calls == ["before", "search", "after"]

    # 검색 뒤 무료 소스는 free_new 에도 안 들어간다 — skip_ge=2 에서 before(1건)만으론 스킵 불발.
    srcs = _srcs()
    discover_segment(
        SEG, _settings(discovery_source_workers=1, search_skip_if_free_ge=2), sources=srcs
    )
    assert srcs[1].calls == 1  # after(z1.com)가 카운트됐다면 2>=2 로 스킵됐을 것.


def test_parallel_region_segment_stays_search_only() -> None:
    """지역 세그먼트는 병렬이어도 검색 전용 — 무료 소스는 호출조차 안 된다."""
    free = _Stub("free", [_dc("reg:a1", "a1.com")])
    spy = _SpySearch(_settings(), results=[])
    seg = Segment(country="KR", industry="제조", region="서울")
    discover_segment(seg, _settings(discovery_source_workers=4), sources=[free, spy])
    assert free.threads == [] and spy.calls == 1
