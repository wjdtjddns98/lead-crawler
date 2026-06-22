"""OpenCorporates(글로벌 법인 집계원) 발견 소스 — 다국가 등록기업.

전 세계 등록처 데이터를 모은 집계원(Tier A, 유료 토큰). dry_run 은 네트워크 없이
결정적 더미, 라이브는 companies/search 로 ``country_code`` 별 질의 후 폐업(inactive)
법인을 제외한다(제약 ② — 실존 기업만). 업종 정밀 필터는 등록처별 코드 체계가 달라
베스트에포트(검색어 q)로만 적용한다.

집계 레코드엔 웹사이트가 없어 라이브 도메인은 None — 도메인 보강은 enrich 단계로
넘긴다(GLEIF 와 동일 규약). canonical_key 는 ``reg:opencorporates:<관할/번호>`` 로
안정적이다(제약 ①). ``opencorporates_api_key`` 가 없으면 비활성(no-op).

테스트는 :class:`SupportsFetch` 가짜 페처를 주입해 네트워크 없이 파싱을 검증한다.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company
from .countries import resolve_country
from .http import Fetcher, SupportsFetch
from .industry import industry_search_term

log = get_logger("sources.opencorporates")

_SEARCH_URL = "https://api.opencorporates.com/v0.4/companies/search"
_PER_PAGE = 100  # OpenCorporates per_page 상한.
# 예산/레이트리밋 보호 가드: 페이지를 무한정 넘기지 않도록 절대 상한.
_MAX_PAGES = 20


class OpenCorporatesSource:
    """OpenCorporates 기반 다국가 등록기업 발견 소스(국가별)."""

    name = "opencorporates"

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
        """ISO2 해석 가능한 국가 세그먼트에 적용된다(집계원 — 업종 무관)."""
        return resolve_country(segment.country) is not None

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트 국가의 실존(미폐업) 법인 목록을 반환한다."""
        if self._settings.dry_run:
            return self._dry(segment)
        if not self._settings.opencorporates_api_key:
            log.info("opencorporates.skip.no_key")
            return []
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(registry_id 기반 canonical_key).

        registry_id 에 국가를 넣어야 전 국가 적용 소스가 다국가 dry 시뮬레이션에서
        국가 간 충돌(dedup 소멸)하지 않는다(GLEIF 더미와 동일 규약).
        """
        cc = (segment.country or "xx").strip().lower()
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} OpenCorp {i}",
                domain=f"{cc}-oc{i}.com",
                registry="opencorporates",
                registry_id=f"{cc}/{i:06d}",
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

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """실 OpenCorporates 발견(국가 필터 + inactive 제외 + 페이징 + 캡)."""
        country = resolve_country(segment.country)
        if country is None:  # applies_to 가 보장하지만 방어적.
            return []
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        token = self._settings.opencorporates_api_key
        # 업종 베스트에포트 검색어 — 라틴/영어 중심 색인이라 한글을 영어로 옮긴다(집계원엔
        # 통일 업종코드 없음). 매핑 없는 업종은 원문 그대로(near-zero 매칭 가능, 베스트에포트).
        q = industry_search_term(segment.industry)

        out: list[DiscoveredCompany] = []
        page = 1
        while len(out) < cap and page <= _MAX_PAGES:
            params = {
                "q": q,
                "country_code": country.iso2.lower(),
                "api_token": token,
                "page": page,
                "per_page": min(_PER_PAGE, cap),
            }
            try:
                payload = fetcher.get_json(_SEARCH_URL, params=params)
            except Exception as exc:  # 키오류·API오류·네트워크 → 부분결과 보존 후 중단.
                # api_token 은 쿼리스트링으로 가므로 httpx 오류문자열에 섞일 수 있어 레다크션.
                log.info("opencorporates.error", page=page, err=_redact(str(exc), token))
                break
            companies = _companies(payload)
            if not companies:
                break
            for wrapped in companies:
                dc = self._candidate(segment, wrapped)
                if dc is not None:
                    out.append(dc)
                    if len(out) >= cap:
                        break
            page += 1
        log.info("opencorporates.live", segment=segment.label, n=len(out))
        return out

    def _candidate(self, segment: Segment, wrapped: Any) -> DiscoveredCompany | None:
        """검색 항목 1건을 후보로 변환(폐업/형식불일치는 제외)."""
        if not isinstance(wrapped, dict):
            return None
        company = wrapped.get("company")
        if not isinstance(company, dict):
            return None
        # 제약 ②: 폐업(inactive=True) 법인 제외. inactive 가 null/누락인 관할(상태 미추적)은
        # 여기서 통과시키되, 최종 실존 판정은 다운스트림 verify(DNS+HTTP 생존)가 책임진다 —
        # 즉 제약 ②는 '소스 단독'이 아니라 '파이프라인 전체'로 충족된다(보수적 admit).
        if company.get("inactive") is True:
            return None
        number = company.get("company_number")
        jurisdiction = company.get("jurisdiction_code")
        name = company.get("name")
        if not number or not jurisdiction or not name:
            return None
        return build_company(
            source=self.name,
            segment=segment,
            name=str(name),
            domain=None,  # 집계 레코드엔 웹사이트 없음 → enrich 단계에서 보강.
            registry="opencorporates",
            registry_id=f"{jurisdiction}/{number}",
        )


def _redact(text: str, secret: str) -> str:
    """오류 문자열에서 비밀값(api_token)을 가린다(로그 유출 방지)."""
    return text.replace(secret, "***") if secret else text


def _companies(payload: Any) -> list[Any]:
    """OpenCorporates 응답에서 companies 목록을 안전하게 추출한다(형식불일치 시 빈 목록)."""
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, dict):
        return []
    companies = results.get("companies")
    return companies if isinstance(companies, list) else []
