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


def close_sources(sources: list[DiscoverySource]) -> None:
    """발견 소스들이 내부에 만든 httpx 클라이언트(Fetcher)를 정리한다(누수 방지).

    소스는 ``self._fetcher`` 를 지연 생성하지만 close() 를 노출하지 않으므로, 그 내부
    fetcher 의 close 만 best-effort 로 호출한다(없으면 no-op). 런 종료 시 1회 호출한다.
    """
    for src in sources:
        fetcher = getattr(src, "_fetcher", None)
        close = getattr(fetcher, "close", None)
        if callable(close):
            close()


def discover_segment(
    segment: Segment,
    settings: Settings | None = None,
    cost_ledger: SupportsCostLedger | None = None,
    *,
    sources: list[DiscoverySource] | None = None,
    seen_domains: set[str] | None = None,
) -> list[DiscoveredCompany]:
    """세그먼트에 적용 가능한 소스들을 실행해 중복 없는 후보 목록을 반환한다.

    dedup 은 **2중 동치**로 한다(제약 ①, '첫 등장 우선' = 신뢰도 높은 소스 우선):
    - canonical_key 동치(같은 등록처 식별자/도메인 key),
    - 도메인 동치 — 같은 실존 기업이 등록처 소스(reg:..., 도메인 보유)와 검색 소스
      (dom:...)에서 서로 다른 key 로 잡혀도 정규화 도메인이 같으면 하나로 병합한다.

    주의: 여기서의 세그먼트-내부 도메인 동치는 **세그먼트 1건 내부**에서만 적용된다.
    세그먼트를 가로지르거나 DB 영속을 거친 cross-run 중복은 파이프라인
    (:func:`run_pipeline`)이 런 전체 ``seen``/``seen_domains`` 로 처리한다.

    ``seen_domains`` 가 주어지면(글로벌 정규화 도메인 집합 — DB시드+런 누적) 유료 검색
    소스에 (글로벌 ∪ 이번 세그먼트 무료소스 도메인)을 주입해, 이미 아는 도메인에 검색비를
    쓰지 않게 한다(제약 ①, 비용 가드). 또 ``search_skip_if_free_ge`` (>0) 이면 무료 소스가
    이 세그먼트에서 신규를 그만큼 찾았을 때 유료 검색 호출 자체를 건너뛴다(②, 무료 우선).
    다른(무료) 소스의 호출 시그니처는 바뀌지 않는다.

    ``sources`` 가 주어지면 그 인스턴스를 재사용한다(파이프라인이 런 시작에 1회 빌드해
    모든 세그먼트에 넘김 — 세그먼트마다 Fetcher 재생성·httpx 누수를 막고 keep-alive
    연결을 재사용). 미지정 시 매 호출 build_sources(직접/테스트 호출 하위호환).
    발견 루프는 단일 스레드라(파이프라인 main 스레드) 소스 공유에 경합이 없다.
    """
    settings = settings or get_settings()
    src_list = sources if sources is not None else build_sources(settings, cost_ledger)
    skip_ge = settings.search_skip_if_free_ge
    out: list[DiscoveredCompany] = []
    seen_keys: set[str] = set()
    seg_domains: set[str] = set()  # 이번 세그먼트 내부 dedup 도메인.
    free_new = 0  # 무료(비검색) 소스가 이 세그먼트에서 찾은 글로벌-신규 수(② 스킵 판단).
    for src in src_list:
        if not src.applies_to(segment):
            continue
        if isinstance(src, SearchSource):
            # ② 무료 소스가 이미 충분히 커버 → 유료 검색 호출 자체 스킵(Serper 1콜/세그먼트 절감).
            if skip_ge > 0 and free_new >= skip_ge:
                log.info("search.skip.free_covered", segment=segment.label, free_new=free_new)
                continue
            # ① 글로벌 + 이번 세그먼트 무료소스 도메인을 seen 으로 주입(중복 도메인 비과금).
            inject = (seen_domains | seg_domains) if seen_domains is not None else seg_domains
            found = src.discover(segment, seen=inject)
        else:
            found = src.discover(segment)
        log.info("source.discover", source=src.name, segment=segment.label, n=len(found))
        for dc in found:
            if dc.canonical_key in seen_keys:
                continue
            dom = normalize_domain(dc.domain) if dc.domain else None
            if dom is not None and dom in seg_domains:
                # 다른 key 지만 같은 도메인 → 이미 더 신뢰도 높은 소스로 잡힌 동일 기업.
                continue
            seen_keys.add(dc.canonical_key)
            if dom is not None:
                seg_domains.add(dom)
            out.append(dc)
            # 무료 소스의 글로벌-신규 발견만 ② 스킵 판단에 카운트한다. 검색 소스는 제외하고,
            # 도메인 없는(name: 티어) 후보도 제외 — enrich 전이라 '커버됨'으로 보면 도메인 있는
            # 실리드를 찾았을 유료검색을 잘못 스킵할 수 있다(아키텍트 권고: enrichable 만 커버로).
            if (
                not isinstance(src, SearchSource)
                and dom is not None
                and (seen_domains is None or dom not in seen_domains)
            ):
                free_new += 1
    return out
