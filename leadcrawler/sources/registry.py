"""발견 소스 레지스트리 — 세그먼트별 적용 소스 선택·병합·중복제거.

파이프라인은 :func:`discover_segment` 만 호출하면 된다. 등록된 소스 중 해당
세그먼트에 ``applies_to`` 인 것만 실행하고, 결과를 ``canonical_key`` 로 합쳐(제약 ①)
하나의 후보 목록으로 반환한다.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ..logging import get_logger
from .base import DiscoveredCompany, DiscoverySource, Segment
from .dart import DartSource
from .edgar import EdgarSource
from .exchanges import PseSource, SetSource
from .gleif import GleifSource
from .search import SearchSource
from .wikidata import WikidataSource

log = get_logger("sources.registry")


def build_sources(settings: Settings) -> list[DiscoverySource]:
    """등록된 발견 소스 인스턴스 목록을 만든다(우선순위 순).

    순서 = canonical_key '첫 등장 우선' 신뢰도 순서: 등록처·거래소(EDGAR/DART/
    PSE/SET, reg: 키) → 글로벌 집계원(GLEIF/Wikidata, reg: 키) → 검색(dom: 키, 가장 약함).
    """
    return [
        EdgarSource(settings),
        DartSource(settings),
        PseSource(settings),
        SetSource(settings),
        GleifSource(settings),
        WikidataSource(settings),
        SearchSource(settings),
    ]


def discover_segment(
    segment: Segment, settings: Settings | None = None
) -> list[DiscoveredCompany]:
    """세그먼트에 적용 가능한 소스들을 실행해 중복 없는 후보 목록을 반환한다."""
    # TODO(M2): 현재 dedup 은 canonical_key '첫 등장 우선'이라, 같은 실존 기업이
    # 등록처 소스(reg:...)와 검색 소스(dom:...)에서 서로 다른 key 로 잡히면 병합되지
    # 않고 중복 산출될 수 있다. 라이브 소스 도입 시 식별자 동치 기반 진짜 머지 필요.
    settings = settings or get_settings()
    out: list[DiscoveredCompany] = []
    seen: set[str] = set()
    for src in build_sources(settings):
        if not src.applies_to(segment):
            continue
        found = src.discover(segment)
        log.info("source.discover", source=src.name, segment=segment.label, n=len(found))
        for dc in found:
            if dc.canonical_key in seen:
                continue
            seen.add(dc.canonical_key)
            out.append(dc)
    return out
