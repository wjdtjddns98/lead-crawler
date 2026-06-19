"""거래소 상장목록 발견 소스(Tier B) — 국가별 상장기업의 권위·유한 소스.

각 증권거래소는 전체 상장사 목록을 공개한다(나라당 수백~수천 건, 유한). 등록처
(EDGAR/DART)와 동일하게 ``reg:<exchange>:<symbol>`` 키로 신뢰도 높게 잡힌다.
산출물의 상장여부는 항상 ``listed``(상장목록이므로). 업종 필터는 하지 않는다
(거래소 목록은 전 섹터 — 업종 정제는 다운스트림).

라이브 엔드포인트는 2026-06-19 실연동 검증함:
- PSE(필리핀): ``companyDirectory/search.ax`` — POST 폼 + HTML 테이블(JSON 아님),
  ``pageNo`` 로 페이지네이션(50/page, 총 ~283사). 인증·WAF 없음 → 정적 스크래핑 동작.
- SET(태국): 공개 API 가 Incapsula WAF(403)로 정적 HTTP 차단됨 → 라이브 비활성.
  태국 상장사는 당분간 Tier A(GLEIF/Wikidata)로 커버. 실연동은 헤드리스/대체소스 필요.

dry_run: 네트워크 없는 결정적 더미(전 소스 공통). 키 불필요.
"""

from __future__ import annotations

import re

from ..config import Settings
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company, is_country
from .http import Fetcher, SupportsFetch

log = get_logger("sources.exchanges")


class ExchangeSource:
    """거래소 상장목록 발견 소스의 공통 베이스(서브클래스가 국가·엔드포인트·_live 제공)."""

    name: str = ""
    registry: str = ""
    countries: frozenset[str] = frozenset()

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
        """해당 거래소 국가 세그먼트에 적용된다(상장여부 무관 — 산출은 항상 listed)."""
        return is_country(segment, self.countries)

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트 국가의 상장기업 목록을 반환한다."""
        if self._settings.dry_run:
            return self._dry(segment)
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(registry_id 기반 canonical_key + 도메인)."""
        cc = (segment.country or "xx").strip().lower()
        listed_seg = Segment(country=segment.country, industry=segment.industry, listed="listed")
        return [
            build_company(
                source=self.name,
                segment=listed_seg,
                name=f"{segment.industry} {self.name.upper()} 상장사 {i}",
                domain=f"{cc}-{self.name}{i}.com",
                registry=self.registry,
                registry_id=f"{self.name.upper()}{i:04d}",
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
        """실 거래소 발견(서브클래스가 거래소별로 구현)."""
        raise NotImplementedError


# PSE 상장사 행: cmDetail('cmpyId','secId')">회사명</a></td> <td..><a..>심볼</a> 순.
_PSE_ROW = re.compile(
    r"cmDetail\('(\d+)','(\d+)'\);return false;\">([^<]+)</a></td>\s*"
    r"<td[^>]*><a[^>]*>([^<]+)</a>",
    re.S,
)
# PSE 페이지네이션 절대 상한(예산 보호) — 실제는 ~6페이지(50/page).
_PSE_MAX_PAGES = 50


class PseSource(ExchangeSource):
    """필리핀 증권거래소(PSE) 상장목록 소스(POST 폼 + HTML 파싱)."""

    name = "pse"
    registry = "pse"
    countries = frozenset({"ph", "phl", "philippines", "필리핀"})
    list_url = "https://edge.pse.com.ph/companyDirectory/search.ax"

    @staticmethod
    def _form(page: int) -> dict[str, str]:
        """PSE companyDirectory 검색 폼 파라미터(회사명 오름차순)."""
        return {
            "pageNo": str(page),
            "companyId": "",
            "keyword": "",
            "sortType": "cmpy",
            "dateSortType": "ASC",
            "cmpyTypeId": "",
            "symbolType": "",
        }

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """PSE companyDirectory 를 페이지네이션하며 상장사를 수집한다(symbol dedup + 캡)."""
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        listed_seg = Segment(country=segment.country, industry=segment.industry, listed="listed")

        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        page = 1
        while len(out) < cap and page <= _PSE_MAX_PAGES:
            try:
                html = fetcher.post_text(self.list_url, data=self._form(page))
            except Exception as exc:  # 응답/네트워크 이상 → 부분 결과 보존 후 중단.
                log.info("pse.error", page=page, err=str(exc))
                break
            rows = _PSE_ROW.findall(html)
            if not rows:  # 빈 페이지 → 마지막 도달.
                break
            for _cmpy_id, _sec_id, name, symbol in rows:
                symbol, name = symbol.strip(), name.strip()
                if not symbol or not name or symbol in seen:
                    continue
                seen.add(symbol)
                out.append(
                    build_company(
                        source=self.name,
                        segment=listed_seg,
                        name=name,
                        domain=None,  # 목록엔 웹사이트 없음 → enrich 단계에서 보강.
                        registry=self.registry,
                        registry_id=symbol,
                    )
                )
                if len(out) >= cap:
                    break
            page += 1
        log.info("pse.live", segment=segment.label, n=len(out))
        return out


class SetSource(ExchangeSource):
    """태국 증권거래소(SET) 상장목록 소스 — 라이브는 WAF 차단으로 비활성(dry 전용)."""

    name = "set"
    registry = "set"
    countries = frozenset({"th", "tha", "thailand", "태국"})

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """SET 공개 API 는 Incapsula WAF(403)로 정적 HTTP 차단(2026-06-19 확인).

        실연동 전까지 라이브는 네트워크 없이 빈 결과(태국은 Tier A 로 커버).
        TODO(live): 헤드리스 브라우저 또는 대체 데이터소스(SEC Thailand 등) 필요.
        """
        log.info("set.skip.waf_blocked", segment=segment.label)
        return []
