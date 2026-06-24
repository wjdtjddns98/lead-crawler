"""발견 소스 레지스트리 — 세그먼트별 적용 소스 선택·병합·중복제거.

파이프라인은 :func:`discover_segment` 만 호출하면 된다. 등록된 소스 중 해당
세그먼트에 ``applies_to`` 인 것만 실행하고, 결과를 ``canonical_key`` 로 합쳐(제약 ①)
하나의 후보 목록으로 반환한다.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ..cost_ledger import SupportsCostLedger
from ..logging import get_logger
from ..dedup import normalize_domain
from .base import DiscoveredCompany, DiscoverySource, Segment
from .companieshouse import CompaniesHouseSource
from .dart import DartSource
from .edgar import EdgarSource
from .exchanges import BursaSource, IdxSource, PseSource, SetSource, SgxSource
from .gleif import GleifSource
from .opencorporates import OpenCorporatesSource
from .search import SearchSource
from .wikidata import WikidataSource

log = get_logger("sources.registry")


def build_sources(
    settings: Settings, cost_ledger: SupportsCostLedger | None = None
) -> list[DiscoverySource]:
    """등록된 발견 소스 인스턴스 목록을 만든다(우선순위 순).

    순서 = canonical_key '첫 등장 우선' 신뢰도 순서: 등록처·거래소(EDGAR/DART/
    CompaniesHouse/PSE/SET, reg: 키) → 글로벌 집계원(GLEIF/Wikidata/OpenCorporates,
    reg: 키) → 검색(dom: 키, 가장 약함). ``cost_ledger`` 는 유료 검색(Serper) 과금
    추적용으로 SearchSource 에 주입된다.
    """
    return [
        EdgarSource(settings),
        DartSource(settings),
        CompaniesHouseSource(settings),
        PseSource(settings),
        SetSource(settings),
        SgxSource(settings),
        IdxSource(settings),
        BursaSource(settings),
        GleifSource(settings),
        WikidataSource(settings),
        OpenCorporatesSource(settings),
        SearchSource(settings, cost_ledger=cost_ledger),
    ]


def discover_segment(
    segment: Segment,
    settings: Settings | None = None,
    cost_ledger: SupportsCostLedger | None = None,
) -> list[DiscoveredCompany]:
    """세그먼트에 적용 가능한 소스들을 실행해 중복 없는 후보 목록을 반환한다.

    dedup 은 **2중 동치**로 한다(제약 ①, '첫 등장 우선' = 신뢰도 높은 소스 우선):
    - canonical_key 동치(같은 등록처 식별자/도메인 key),
    - 도메인 동치 — 같은 실존 기업이 등록처 소스(reg:..., 도메인 보유)와 검색 소스
      (dom:...)에서 서로 다른 key 로 잡혀도 정규화 도메인이 같으면 하나로 병합한다.

    주의: 여기서의 도메인 동치는 **세그먼트 1건 내부**에서만 적용된다. 세그먼트를
    가로지르거나(다국가/다업종) DB 영속을 거친 cross-run 중복은 파이프라인
    (:func:`run_pipeline`)이 런 전체 ``seen``/``seen_domains`` 로 처리한다.
    """
    settings = settings or get_settings()
    out: list[DiscoveredCompany] = []
    seen_keys: set[str] = set()
    seen_domains: set[str] = set()
    for src in build_sources(settings, cost_ledger):
        if not src.applies_to(segment):
            continue
        found = src.discover(segment)
        log.info("source.discover", source=src.name, segment=segment.label, n=len(found))
        for dc in found:
            if dc.canonical_key in seen_keys:
                continue
            dom = normalize_domain(dc.domain) if dc.domain else None
            if dom is not None and dom in seen_domains:
                # 다른 key 지만 같은 도메인 → 이미 더 신뢰도 높은 소스로 잡힌 동일 기업.
                continue
            seen_keys.add(dc.canonical_key)
            if dom is not None:
                seen_domains.add(dom)
            out.append(dc)
    return out
