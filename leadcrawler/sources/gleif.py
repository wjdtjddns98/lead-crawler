"""GLEIF LEI 발견 소스 — 전 세계 법인 식별자(무료, API 키 불필요).

글로벌 집계원(Tier A): 단일 연동으로 수백 개국을 커버한다. dry_run 은 네트워크
없이 결정적 더미, 라이브는 GLEIF 공개 API(JSON:API)
``lei-records?filter[entity.legalAddress.country]=<ISO2>`` 를 국가별로 페이징하며
``entity.status == ACTIVE`` 인 법인만 수집한다(제약 ② — 실존 기업만).

LEI 레코드에는 웹사이트가 없어 라이브 도메인은 None — 도메인 보강은 enrich 단계로
넘긴다(그 전까지는 site 신호 부재로 실존 미확정일 수 있음, 향후 등록처 active 신호로 보완).
canonical_key 는 ``reg:lei:<LEI>`` 로 안정적이다(제약 ①). 업종 필터는 하지 않는다
(집계원 tier — 업종 정제는 다운스트림). 국가는 :mod:`countries` 로 ISO2 해석
가능한 세그먼트에만 적용된다(미등록 국가는 검색 소스로 폴백).
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company
from .countries import resolve_country
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import is_specific_industry

log = get_logger("sources.gleif")

_API_URL = "https://api.gleif.org/api/v1/lei-records"
_PAGE = 200  # GLEIF page[size] 최대 200.
# 예산 보호 가드: 서버 ACTIVE 필터가 어긋나(전량 클라이언트 제외) cap 이 안 차도
# 페이지를 무한정 넘기지 않도록 절대 상한. 정상 경로는 ceil(cap/page) 페이지면 끝난다.
_MAX_PAGES = 20


class GleifSource:
    """GLEIF LEI 기반 전 세계 법인 발견 소스(국가별)."""

    name = "gleif"

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
        """ISO2 해석 가능한 국가 세그먼트에 적용된다. 단 구체 업종 지정 시엔 제외 —
        GLEIF 는 업종 필터를 못 해 비대상 업종을 섞으므로(정밀도 우선, 등록처/검색에 위임)."""
        return resolve_country(segment.country) is not None and not is_specific_industry(
            segment.industry
        )

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트 국가의 실존(ACTIVE) 법인 목록을 반환한다."""
        if self._settings.dry_run:
            return self._dry(segment)
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(registry_id 기반 canonical_key).

        라이브 LEI 레코드엔 도메인이 없지만, dry 더미는 '실존 active 기업' 시뮬레이션을
        위해 도메인을 부여한다(EDGAR/DART 더미와 동일 규약 — dry 전부 active).
        """
        cc = (segment.country or "xx").strip().lower()
        # registry_id 에 국가를 넣어야 전 국가 적용 소스가 다국가 dry 시뮬레이션에서
        # 국가 간 충돌(dedup 소멸)하지 않는다(실 LEI 도 국가별로 다름).
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} GLEIF Co {i}",
                domain=f"{cc}-lei{i}.com",
                registry="lei",
                registry_id=f"LEI-{cc}-{i}",
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
        """실 GLEIF 발견(국가 필터 + ACTIVE 필터 + 페이징 + 캡)."""
        country = resolve_country(segment.country)
        if country is None:  # applies_to 가 보장하지만 방어적.
            return []
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source

        out: list[DiscoveredCompany] = []
        page = 1
        while len(out) < cap and page <= _MAX_PAGES:
            params = {
                "filter[entity.legalAddress.country]": country.iso2,
                "filter[entity.status]": "ACTIVE",
                "page[size]": min(_PAGE, cap),
                "page[number]": page,
            }
            try:
                payload = fetcher.get_json(_API_URL, params=params)
            except Exception as exc:  # 깨진 응답/네트워크 → 부분 결과 보존 후 중단.
                log.info("gleif.error", page=page, err=str(exc))
                break
            records = payload.get("data") if isinstance(payload, dict) else None
            if not records:
                break
            for rec in records:
                dc = self._candidate(segment, rec)
                if dc is not None:
                    out.append(dc)
                    if len(out) >= cap:
                        break
            page += 1
        log.info("gleif.live", segment=segment.label, n=len(out))
        return out

    def _candidate(self, segment: Segment, rec: Any) -> DiscoveredCompany | None:
        """LEI 레코드 1건을 후보로 변환(ACTIVE 외/형식 불일치는 제외)."""
        if not isinstance(rec, dict):
            return None
        attrs = rec.get("attributes")
        if not isinstance(attrs, dict):
            return None
        entity = attrs.get("entity")
        if not isinstance(entity, dict):
            return None
        # 제약 ②: 실존(ACTIVE) 법인만(서버 필터 + 방어적 재확인).
        if str(entity.get("status", "")).upper() != "ACTIVE":
            return None
        legal_name = entity.get("legalName")
        name = legal_name.get("name") if isinstance(legal_name, dict) else None
        lei = rec.get("id") or attrs.get("lei")
        if not name or not lei:
            return None
        return build_company(
            source=self.name,
            segment=segment,
            name=str(name),
            domain=None,  # LEI 레코드엔 웹사이트 없음 → enrich 단계에서 보강.
            registry="lei",
            registry_id=str(lei),
        )
