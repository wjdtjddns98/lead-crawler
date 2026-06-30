"""Wikidata 발견 소스 — 국가별 기업 + 공식 웹사이트(무료 SPARQL, 키 불필요).

글로벌 집계원(Tier A): GLEIF 대비 강점은 공식 웹사이트(P856)를 제공해 도메인을
바로 확보한다는 것(검색 소스보다 신뢰도 높음). dry_run 은 네트워크 없이 결정적
더미, 라이브는 Wikidata Query Service(SPARQL)에 국가(P17)별 기업
(P31/P279* business enterprise) 질의로 회사명·웹사이트를 수집한다.

canonical_key 는 ``reg:wikidata:<QID>`` 로 안정적이다(제약 ①). WMF 정책상 식별
가능한 User-Agent 가 필요하다(``discovery_user_agent``). 국가는 :mod:`countries`
로 Wikidata QID 해석 가능한 세그먼트에만 적용된다(미등록 국가는 검색 소스로 폴백).
업종 필터는 하지 않는다(집계원 tier — 업종 정제는 다운스트림).
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..dedup import normalize_domain
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company
from .countries import resolve_country
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import is_specific_industry

log = get_logger("sources.wikidata")

_SPARQL_URL = "https://query.wikidata.org/sparql"
# 다중 웹사이트(P856) 엔티티는 ?item 당 여러 행이 와 seen dedup 으로 접히므로,
# 고유 기업 cap 을 채우려면 LIMIT 에 오버페치 마진을 둔다.
_OVERFETCH = 3
# 기업(business enterprise Q4830453 및 하위) 중 해당 국가(P17) — 공식 웹사이트는 선택.
_QUERY = """SELECT ?item ?itemLabel ?website WHERE {{
  ?item wdt:P31/wdt:P279* wd:Q4830453 .
  ?item wdt:P17 wd:{qid} .
  OPTIONAL {{ ?item wdt:P856 ?website. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT {limit}"""


class WikidataSource:
    """Wikidata SPARQL 기반 전 세계 기업 발견 소스(국가별)."""

    name = "wikidata"

    def __init__(
        self,
        settings: Settings,
        *,
        count: int = 2,
        fetcher: SupportsFetch | None = None,
        rate_limiters: HostRateLimiters | None = None,
    ) -> None:
        self._settings = settings
        self._count = count
        self._fetcher = fetcher
        self._rate_limiters = rate_limiters

    def applies_to(self, segment: Segment) -> bool:
        """QID 해석 가능한 국가 세그먼트에 적용된다. 단 구체 업종 지정 시엔 제외 —
        Wikidata 질의가 업종 필터를 안 해 비대상 업종을 섞으므로(정밀도 우선)."""
        return resolve_country(segment.country) is not None and not is_specific_industry(
            segment.industry
        )

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트 국가의 기업 목록을 반환한다(공식 웹사이트 보유 우선 가치)."""
        if self._settings.dry_run:
            return self._dry(segment)
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(registry_id 기반 canonical_key + 도메인)."""
        cc = (segment.country or "xx").strip().lower()
        # registry_id 에 국가를 넣어야 전 국가 적용 소스가 다국가 dry 시뮬레이션에서
        # 국가 간 충돌(dedup 소멸)하지 않는다(실 QID 도 기업마다 다름).
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} 위키데이터 {i}",
                domain=f"{cc}-wiki{i}.com",
                registry="wikidata",
                registry_id=f"Q-{cc}-{i}",
            )
            for i in range(self._count)
        ]

    def _client(self) -> SupportsFetch:
        # 소스 인스턴스당 1개만 생성·재사용(discover 호출마다 클라이언트 누수 방지).
        if self._fetcher is None:
            self._fetcher = Fetcher(
                user_agent=self._settings.discovery_user_agent,
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
                rate_limiters=self._rate_limiters,
            )
        return self._fetcher

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """실 Wikidata 발견(SPARQL 질의 + QID dedup + 캡)."""
        country = resolve_country(segment.country)
        if country is None:  # applies_to 가 보장하지만 방어적.
            return []
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        # 다중 웹사이트 행으로 인한 cap 미달을 막기 위해 LIMIT 은 오버페치(고유 cap 은 유지).
        query = _QUERY.format(qid=country.qid, limit=cap * _OVERFETCH)
        try:
            payload = fetcher.get_json(
                _SPARQL_URL,
                params={"query": query, "format": "json"},
                headers={"Accept": "application/sparql-results+json"},
            )
        except Exception as exc:  # 질의 실패/타임아웃 → 빈 결과(배치 보호).
            log.info("wikidata.error", segment=segment.label, err=str(exc))
            return []

        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        for row in _bindings(payload):
            dc = self._candidate(segment, row, seen)
            if dc is not None:
                out.append(dc)
                if len(out) >= cap:
                    break
        log.info("wikidata.live", segment=segment.label, n=len(out))
        return out

    def _candidate(
        self, segment: Segment, row: Any, seen: set[str]
    ) -> DiscoveredCompany | None:
        """SPARQL 바인딩 1건을 후보로 변환(QID dedup·라벨 미해소 제외)."""
        if not isinstance(row, dict):
            return None
        uri = _cell(row, "item")
        if not uri:
            return None
        qid = uri.rsplit("/", 1)[-1]  # http://www.wikidata.org/entity/Q123 → Q123
        if not qid or qid in seen:
            return None
        name = _cell(row, "itemLabel")
        if not name or name == qid:  # 라벨 미해소 시 QID 가 라벨로 옴 → 스킵.
            return None
        seen.add(qid)
        return build_company(
            source=self.name,
            segment=segment,
            name=name,
            domain=normalize_domain(_cell(row, "website")),
            registry="wikidata",
            registry_id=qid,
        )


def _bindings(payload: Any) -> list[Any]:
    """SPARQL JSON 결과에서 results.bindings 리스트를 안전 추출한다."""
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, dict):
        return []
    bindings = results.get("bindings")
    return bindings if isinstance(bindings, list) else []


def _cell(row: dict, key: str) -> str | None:
    """SPARQL 바인딩 셀({"value": ...})에서 문자열 값을 안전 추출한다."""
    cell = row.get(key)
    if isinstance(cell, dict):
        value = cell.get("value")
        if isinstance(value, str) and value:
            return value
    return None
