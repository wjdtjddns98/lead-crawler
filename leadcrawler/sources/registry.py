"""발견 소스 레지스트리 — 세그먼트별 적용 소스 선택·병합·중복제거.

파이프라인은 :func:`discover_segment` 만 호출하면 된다. 등록된 소스 중 해당
세그먼트에 ``applies_to`` 인 것만 실행하고, 결과를 ``canonical_key`` 로 합쳐(제약 ①)
하나의 후보 목록으로 반환한다.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..config import Settings, get_settings
from ..cost_ledger import SupportsCostLedger
from ..logging import get_logger
from ..dedup import normalize_domain
from .base import (
    DiscoveredCompany,
    DiscoverySource,
    Segment,
    SupportsCursorStore,
    discovery_chunks,
)
from .http import HostRateLimiters
from .companieshouse import CompaniesHouseSource
from .dart import DartSource, SupportsCorpCache
from .edgar import EdgarSource
from .exchanges import (
    BursaSource,
    HnxSource,
    HoseSource,
    IdxSource,
    PseSource,
    SetSource,
    SgxSource,
)
from .ai_directory import AiDirectorySource
from .gleif import GleifSource
from .naver_local import NaverLocalSource
from .nps import NpsSource, SupportsNpsStore
from .opencorporates import OpenCorporatesSource
from .search import SearchSource
from .wikidata import WikidataSource

log = get_logger("sources.registry")


def build_sources(
    settings: Settings,
    cost_ledger: SupportsCostLedger | None = None,
    rate_limiters: HostRateLimiters | None = None,
    cursor_store: SupportsCursorStore | None = None,
    dart_corp_cache: SupportsCorpCache | None = None,
    nps_store: SupportsNpsStore | None = None,
) -> list[DiscoverySource]:
    """등록된 발견 소스 인스턴스 목록을 만든다(우선순위 순).

    순서 = canonical_key '첫 등장 우선' 신뢰도 순서: 등록처·거래소(EDGAR/DART/
    CompaniesHouse/PSE/SET/SGX/IDX/Bursa/HOSE/HNX, reg: 키) → 글로벌 집계원(GLEIF/Wikidata/OpenCorporates,
    reg: 키) → 검색(dom: 키, 가장 약함). ``cost_ledger`` 는 유료 검색(Serper) 과금
    추적용으로 SearchSource 에 주입된다.

    ``rate_limiters`` 가 주어지면(세그먼트 병렬 발견 시) 각 소스의 내부 Fetcher 가 이
    공유 호스트별 레이트리미터를 쓰게 해, 워커별 독립 소스가 같은 호스트를 동시에 때려도
    합산 발사율을 억제한다(429 선제 방지). None(기본)이면 기존 동작 그대로(회귀 0).

    ``cursor_store`` 가 주어지면(persist 런) 등록처·집계원 소스(EDGAR/DART/CH/GLEIF)가
    런 간 스캔 위치를 영속해 다음 런이 다음 페이지부터 이어받는다(딥백필). None 이면 기존 동작.
    """
    return [
        EdgarSource(settings, rate_limiters=rate_limiters, cursor_store=cursor_store),
        DartSource(
            settings,
            rate_limiters=rate_limiters,
            cursor_store=cursor_store,
            corp_cache=dart_corp_cache,
        ),
        CompaniesHouseSource(settings, rate_limiters=rate_limiters, cursor_store=cursor_store),
        PseSource(settings, rate_limiters=rate_limiters),
        SetSource(settings, rate_limiters=rate_limiters),
        SgxSource(settings, rate_limiters=rate_limiters),
        IdxSource(settings, rate_limiters=rate_limiters),
        BursaSource(settings, rate_limiters=rate_limiters),
        HoseSource(settings, rate_limiters=rate_limiters),
        HnxSource(settings, rate_limiters=rate_limiters),
        GleifSource(settings, rate_limiters=rate_limiters, cursor_store=cursor_store),
        WikidataSource(settings, rate_limiters=rate_limiters),
        # 국민연금 스냅샷(무네트워크) — 살아있는 사업장을 업종·규모(가입자수) 우선으로.
        # name: 키(사업자번호 마스킹) — 집계원 뒤에 둬 등록처 키가 항상 이긴다(첫 등장 우선).
        NpsSource(
            settings,
            nps_store=nps_store,
            cursor_store=cursor_store,
            rate_limiters=rate_limiters,
        ),
        # KR 지역검색(무료) — 업종 키워드로 살아있는 업체 직접 발견(순도·저장전환 高).
        # 집계원 뒤·검색 앞: name: 키 신뢰도는 낮지만 link(도메인) 동봉분은 dom: 키.
        NaverLocalSource(settings, rate_limiters=rate_limiters),
        OpenCorporatesSource(settings, rate_limiters=rate_limiters),
        SearchSource(settings, cost_ledger=cost_ledger, rate_limiters=rate_limiters),
        # AI 디렉토리(dom: 키, 구체 업종 전용 — applies_to 게이팅). 검색과 같은 약한 티어라
        # 등록처·집계원 뒤에 둔다(도메인 동치 dedup 시 등록처가 첫 등장 우선). cost_ledger 는
        # 목록추출 LLM(ai_directory) + 디렉토리 URL 수집 Serper 과금 추적용.
        AiDirectorySource(settings, cost_ledger=cost_ledger, rate_limiters=rate_limiters),
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

    동시성: ``discovery_source_workers > 1`` 이면 **비검색(무료) 소스들의 발견을 청크로**
    스레드풀에서 동시에 실행한다(등록처·집계원은 서로 다른 호스트라 세그먼트 병목의 대부분).
    각 소스의 :func:`base.discovery_chunks` 로 얻은 청크(DART 만 window 를 N구간 분할, 그 외
    1청크=전체)를 전 소스에 걸쳐 flatten 해 하나의 풀에 제출하므로, 큰 세그먼트(DART)가
    형제 소스가 비운 유휴 슬롯까지 점유한다. 병합·dedup·검색 게이팅(①②)·소스별 finalize
    (커서/쿼터)는 결과 수집 후 항상 main 스레드에서 src_list 우선순위 순서대로 수행하므로,
    '첫 등장 우선' dedup 과 무료-우선 검색 스킵 판단은 순차 실행과 결정적으로 동일하다.
    검색 소스는 무료 결과(free_new·도메인 주입)에 의존하므로 병렬 대상에서 제외하고 병합
    시점에 순차 호출한다. **단일 소스 인스턴스가 자기 청크 스레드 여럿에 공유되지만, 청크
    워커(_scan_range 등)는 순수(seen/커서/쿼터 미변형)라 안전**하고, 스레드 간 공유 자원
    (HostRateLimiters·DbCursorStore)은 스레드 안전이다.
    """
    settings = settings or get_settings()
    src_list = sources if sources is not None else build_sources(settings, cost_ledger)
    skip_ge = settings.search_skip_if_free_ge
    # 지역 세그먼트(KR 팬아웃)는 검색 전용 — 등록처·집계원·거래소는 주소로 열거하지
    # 않으므로 지역마다 같은 스캔을 반복하면 쿼터 낭비 + 커서(label 키) 파편화만 남는다.
    # 그 소스들은 지역 없는 기본 세그먼트에서 1회만 돈다.
    applicable = [
        src
        for src in src_list
        if (segment.region is None or getattr(src, "region_aware", isinstance(src, SearchSource)))
        and src.applies_to(segment)
    ]
    free_srcs = [src for src in applicable if not isinstance(src, SearchSource)]
    workers = min(settings.discovery_source_workers, len(free_srcs))
    found_by_src: dict[int, list[DiscoveredCompany]] = {}
    if workers > 1:
        # 각 소스의 (청크 콜러블, finalize) 를 얻어(DART 만 window 를 N구간 분할, 그 외 1청크=
        # 전체) 전 소스 청크를 하나의 소스풀에 flatten 제출한다 — 큰 세그먼트(DART)가 형제
        # 소스가 비운 유휴 슬롯까지 점유해 워커 놀림을 없앤다(제약 ①: 청크워커는 순수,
        # seen/커서/쿼터 미변형 — 병합·finalize 는 아래 메인스레드 단독).
        plans = [(src, *discovery_chunks(src, segment)) for src in free_srcs]
        tasks = [
            (id(src), ci, chunk)
            for src, chunks, _fin in plans
            for ci, chunk in enumerate(chunks)
        ]
        per_src: dict[int, list[list[DiscoveredCompany]]] = {
            id(src): [[] for _ in chunks] for src, chunks, _fin in plans
        }
        pool_workers = min(settings.discovery_source_workers, len(tasks))
        log.info(
            "sources.parallel", segment=segment.label,
            workers=pool_workers, sources=len(free_srcs), chunks=len(tasks),
        )
        if tasks:
            with ThreadPoolExecutor(
                max_workers=pool_workers, thread_name_prefix="src"
            ) as pool:
                futures = {pool.submit(chunk): (sid, ci) for sid, ci, chunk in tasks}
            # with 종료 = 전 청크 완료 대기. 예외는 병합 진입 전에 그대로 전파(세그먼트 실패 —
            # 부분 결과로 조용히 진행하지 않는다). 형제 소스들은 이미 끝까지 돈 뒤라 실패
            # 세그먼트도 형제 비용은 쓴 상태(병렬 비용 비대칭). SearchSource *뒤* 무료 소스
            # (AI 디렉토리)도 선실행되므로 월예산 경계에서 예산게이트 순서가 순차와 다를 수 있음.
            for fut, (sid, ci) in futures.items():
                per_src[sid][ci] = fut.result()
        # finalize(메인스레드 1회, 청크 순서대로) → 커서/쿼터 확정. 그 뒤 소스별로 flatten.
        for src, chunks, finalize in plans:
            results = per_src[id(src)]
            finalize(results)
            found_by_src[id(src)] = [c for chunk_out in results for c in chunk_out]
    # workers<=1 이면 선실행하지 않는다 — 병합 루프가 원래 위치에서 지연 호출해 호출 순서까지
    # 순차 원형과 동일(회귀 0). 특히 검색 뒤에 오는 유료 소스(AI 디렉토리)의 예산게이트
    # 평가 순서가 보존된다.
    out: list[DiscoveredCompany] = []
    seen_keys: set[str] = set()
    seg_domains: set[str] = set()  # 이번 세그먼트 내부 dedup 도메인.
    free_new = 0  # 무료(비검색) 소스가 이 세그먼트에서 찾은 글로벌-신규 수(② 스킵 판단).
    for src in applicable:
        if isinstance(src, SearchSource):
            # ② 무료 소스가 이미 충분히 커버 → 유료 검색 호출 자체 스킵(Serper 1콜/세그먼트 절감).
            if skip_ge > 0 and free_new >= skip_ge:
                log.info("search.skip.free_covered", segment=segment.label, free_new=free_new)
                continue
            # ① 글로벌 + 이번 세그먼트 무료소스 도메인을 seen 으로 주입(중복 도메인 비과금).
            inject = (seen_domains | seg_domains) if seen_domains is not None else seg_domains
            found = src.discover(segment, seen=inject)
        elif id(src) in found_by_src:  # 병렬 선실행분.
            found = found_by_src[id(src)]
        else:  # 순차(workers<=1) — 원래 루프 위치에서 지연 호출(호출 순서 원형 보존).
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
