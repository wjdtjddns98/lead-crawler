"""Companies House(영국 법인등록처) 발견 소스 — UK 등록 기업.

영국 공식 등록처의 무료 API(키 필요, HTTP Basic). dry_run 은 네트워크 없이 결정적
더미, 라이브는 advanced-search 엔드포인트로 ``company_status=active`` 인 법인만 페이징하며
수집한다(제약 ② — 실존 기업만). 업종은 UK SIC 2007 접두 매칭으로 베스트에포트 필터.

등록처 레코드엔 웹사이트가 없어 라이브 도메인은 None — 도메인 보강은 enrich 단계로
넘긴다(GLEIF/PSE 와 동일 규약). canonical_key 는 ``reg:companies_house:<번호>`` 로
안정적이다(제약 ①). ``companies_house_api_key`` 가 없으면 비활성(no-op).

테스트는 :class:`SupportsFetch` 가짜 페처를 주입해 네트워크 없이 파싱을 검증한다.
"""

from __future__ import annotations

import base64
from typing import Any

from ..config import Settings
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company, is_country
from .http import Fetcher, SupportsFetch
from .industry import matches_prefix, uk_sic_prefixes

log = get_logger("sources.companies_house")

_GB = {"gb", "uk", "gbr", "united kingdom", "britain", "영국"}
_SEARCH_URL = "https://api.company-information.service.gov.uk/advanced-search/companies"
_PAGE = 100  # 페이지당 size(API 상한은 5000이나 레이트리밋·예산 보호로 보수적 설정).
# 예산/레이트리밋 보호 가드: 업종 필터가 전량 제외해도 페이지를 무한정 넘기지 않도록 절대 상한.
_MAX_PAGES = 20


class CompaniesHouseSource:
    """Companies House 기반 영국 등록기업 발견 소스."""

    name = "companies_house"

    def __init__(
        self,
        settings: Settings,
        *,
        count: int = 2,
        fetcher: SupportsFetch | None = None,
    ) -> None:
        self._settings = settings
        self._count = count
        self._fetcher = fetcher

    def applies_to(self, segment: Segment) -> bool:
        """영국 세그먼트에 적용된다."""
        return is_country(segment, _GB)

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트에 해당하는 영국 active 법인 목록을 반환한다."""
        if self._settings.dry_run:
            return self._dry(segment)
        if not self._settings.companies_house_api_key:
            log.info("companies_house.skip.no_key")
            return []
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(registry_id 기반 canonical_key).

        라이브 등록처 레코드엔 도메인이 없지만, dry 더미는 '실존 active 기업'
        시뮬레이션을 위해 도메인을 부여한다(다른 등록처 더미와 동일 규약).
        """
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} CH Ltd {i}",
                domain=f"gb-ch{i}.co.uk",
                registry="companies_house",
                registry_id=f"GB{i:08d}",
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
            )
        return self._fetcher

    def _auth_header(self) -> dict[str, str]:
        """API 키를 HTTP Basic(username=key, password 공백) 헤더로 만든다."""
        token = base64.b64encode(f"{self._settings.companies_house_api_key}:".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """실 Companies House 발견(active 필터 + SIC 베스트에포트 + 페이징 + 캡)."""
        fetcher = self._client()
        headers = self._auth_header()
        cap = self._settings.discovery_max_per_source
        prefixes = uk_sic_prefixes(segment.industry)

        out: list[DiscoveredCompany] = []
        start = 0
        page = 0
        while len(out) < cap and page < _MAX_PAGES:
            params = {
                "company_status": "active",  # 제약 ②: 실존 법인만(서버 필터).
                "size": min(_PAGE, cap),
                "start_index": start,
            }
            try:
                payload = fetcher.get_json(_SEARCH_URL, params=params, headers=headers)
            except Exception as exc:  # 키오류·API오류·네트워크 → 부분결과 보존 후 중단.
                log.info("companies_house.error", start=start, err=str(exc))
                break
            items = payload.get("items") if isinstance(payload, dict) else None
            if not items:
                break
            for item in items:
                dc = self._candidate(segment, item, prefixes)
                if dc is not None:
                    out.append(dc)
                    if len(out) >= cap:
                        break
            start += len(items)
            page += 1
        log.info("companies_house.live", segment=segment.label, n=len(out))
        return out

    def _candidate(
        self, segment: Segment, item: Any, prefixes: tuple[str, ...] | None
    ) -> DiscoveredCompany | None:
        """검색 항목 1건을 후보로 변환(active 외/형식불일치/업종불일치는 제외)."""
        if not isinstance(item, dict):
            return None
        # 제약 ②: active 만(서버 필터 + 방어적 재확인).
        if str(item.get("company_status", "")).strip().lower() != "active":
            return None
        number = item.get("company_number")
        name = item.get("company_name")
        if not number or not name:
            return None
        # 업종 베스트에포트: 회사의 SIC 목록 중 하나라도 접두 매칭이면 채택(없으면 전량).
        sic_codes = item.get("sic_codes") or []
        if prefixes is not None and not any(matches_prefix(c, prefixes) for c in sic_codes):
            return None
        return build_company(
            source=self.name,
            segment=segment,
            name=str(name),
            domain=None,  # 등록처 레코드엔 웹사이트 없음 → enrich 단계에서 보강.
            registry="companies_house",
            registry_id=str(number),
        )
