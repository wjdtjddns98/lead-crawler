"""거래소 상장목록 발견 소스(Tier B) — 국가별 상장기업의 권위·유한 소스.

각 증권거래소는 전체 상장사 목록을 공개한다(나라당 수백~수천 건, 유한). 등록처
(EDGAR/DART)와 동일하게 ``reg:<exchange>:<symbol>`` 키로 신뢰도 높게 잡힌다.
산출물의 상장여부는 항상 ``listed``(상장목록이므로). 업종 필터는 하지 않는다
(거래소 목록은 전 섹터 — 업종 정제는 다운스트림).

라이브 엔드포인트 상태:
- PSE(필리핀): ``companyDirectory/search.ax`` — POST 폼 + HTML 테이블(JSON 아님),
  ``pageNo`` 로 페이지네이션(50/page, 총 ~283사). 인증·WAF 없음 → 정적 스크래핑 동작
  (2026-06-19 실연동 검증).
- SET(태국)/Bursa(말레이시아): 공개 목록이 WAF/JS 렌더링으로 정적 HTTP 차단 → 라이브
  비활성(dry 전용). 해당국은 당분간 Tier A(GLEIF/Wikidata)로 커버. 헤드리스/대체소스 필요.
- SGX(싱가포르): ``api.sgx.com/securities`` 공개 JSON(인증 불필요). 응답 축약필드 스키마는
  온네트워크 확인 필요한 베스트에포트 — 파서는 관대·graceful.
- IDX(인도네시아): ``GetCompanyProfiles`` JSON(페이징). 실서비스 Cloudflare 보호 가능성
  있어 graceful(차단 시 빈 결과). 스키마는 베스트에포트.

dry_run: 네트워크 없는 결정적 더미(전 소스 공통). 키 불필요.
"""

from __future__ import annotations

import html
import re
from typing import Any

from ..config import Settings
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company, is_country
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import is_specific_industry

log = get_logger("sources.exchanges")


# 거래소 목록 행(베스트에포트): <a ...>SYMBOL</a></td><td>회사명</td> 형태를 관대히 잡는다.
# 실제 SET/Bursa 마크업은 라이브 확인 필요 — 못 맞으면 graceful 빈 결과(A5 한계 문서).
_LISTING_ROW = re.compile(
    r'<a[^>]*>\s*([A-Z0-9.\-]{1,12})\s*</a>\s*</td>\s*<td[^>]*>\s*([^<]{2,120}?)\s*</td>',
    re.S,
)


class ExchangeSource:
    """거래소 상장목록 발견 소스의 공통 베이스(서브클래스가 국가·엔드포인트·_live 제공)."""

    name: str = ""
    registry: str = ""
    countries: frozenset[str] = frozenset()
    # WAF 차단 소스(SET/Bursa)는 True — enable_bypass 시 _client() 가 InsaneFetcher 를 쓴다.
    bypass_capable: bool = False

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
        """해당 거래소 국가 세그먼트에 적용된다(상장여부 무관 — 산출은 항상 listed). 단 구체
        업종 지정 시엔 제외 — 상장목록은 업종 필터가 없어 비대상 업종을 섞으므로(정밀도 우선)."""
        return is_country(segment, self.countries) and not is_specific_industry(segment.industry)

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
            if self.bypass_capable and self._settings.enable_bypass:
                # WAF 차단 소스 + 우회 활성 → 벤더 엔진 어댑터(미설치 시 graceful 빈 결과).
                from .insane_fetcher import InsaneFetcher

                self._fetcher = InsaneFetcher(timeout=self._settings.http_timeout)
            else:
                self._fetcher = Fetcher(
                    user_agent=self._settings.discovery_user_agent,
                    min_interval=self._settings.http_request_delay,
                    timeout=self._settings.http_timeout,
                    rate_limiters=self._rate_limiters,
                )
        return self._fetcher

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """실 거래소 발견(서브클래스가 거래소별로 구현)."""
        raise NotImplementedError

    def _parse_listing(self, page_html: str, segment: Segment) -> list[DiscoveredCompany]:
        """거래소 목록 HTML 에서 (심볼, 회사명) 행을 관대히 추출한다(graceful·캡 적용).

        구조는 거래소마다 달라 **베스트에포트**다(SgxSource 와 동일 철학) — 정규식이 못 잡는
        형식은 빈 결과로 흘려보낸다. 심볼=registry_id, 도메인은 목록에 없어 None(enrich 위임).
        실제 셀렉터/구조는 라이브 확인이 필요하다(A5 한계 문서 참조).
        """
        cap = self._settings.discovery_max_per_source
        listed_seg = Segment(country=segment.country, industry=segment.industry, listed="listed")
        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        for symbol, name in _LISTING_ROW.findall(page_html or ""):
            # HTML 엔티티 복원(PSE 경로와 일관 — &amp; 등이 회사명에 raw 로 남지 않게).
            symbol, name = html.unescape(symbol.strip()), html.unescape(name.strip())
            if not symbol or not name or symbol in seen:
                continue
            seen.add(symbol)
            out.append(
                build_company(
                    source=self.name, segment=listed_seg, name=name, domain=None,
                    registry=self.registry, registry_id=symbol,
                )
            )
            if len(out) >= cap:
                break
        return out


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
                page_html = fetcher.post_text(self.list_url, data=self._form(page))
            except Exception as exc:  # 응답/네트워크 이상 → 부분 결과 보존 후 중단.
                log.info("pse.error", page=page, err_type=type(exc).__name__, err=str(exc))
                break
            rows = _PSE_ROW.findall(page_html)
            if not rows:  # 빈 페이지 → 마지막 도달.
                break
            for _cmpy_id, _sec_id, name, symbol in rows:
                # HTML 엔티티 해제(예: "A. Soriano &amp; Co." → "&") — 엑셀 출력 품질(§3).
                symbol = html.unescape(symbol.strip())
                name = html.unescape(name.strip())
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
    bypass_capable = True  # Incapsula WAF 차단 → enable_bypass 시 InsaneFetcher 로 우회.
    # 베스트에포트 공개 목록 URL(라이브 셀렉터/구조 확인 필요 — A5).
    list_url = "https://www.set.or.th/en/market/get-quote/stock"

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """SET 공개 목록은 Incapsula WAF(403)로 정적 HTTP 차단(2026-06-19 확인).

        ``enable_bypass`` 시 벤더 엔진(InsaneFetcher)으로 목록 HTML 을 가져와 파싱한다. off
        이면 기존대로 빈 결과(태국은 Tier A 로 커버). 우회 실패/형식불일치도 graceful 빈 결과.
        """
        if not self._settings.enable_bypass:
            log.info("set.skip.bypass_off", segment=segment.label)
            return []
        try:
            html = self._client().get_text(self.list_url)
        except Exception as exc:  # 우회/네트워크/형식 → graceful 빈 결과.
            log.info("set.error", err_type=type(exc).__name__, err=str(exc))
            return []
        rows = self._parse_listing(html, segment)
        log.info("set.bypass", segment=segment.label, found=len(rows))
        return rows


class SgxSource(ExchangeSource):
    """싱가포르 거래소(SGX) 상장목록 소스 — 공개 JSON API(인증 불필요).

    SGX 는 전 상장종목을 ``api.sgx.com/securities`` 로 공개한다(1회 호출, 페이징 없음).
    응답 스키마(축약 필드명)는 온네트워크 확인이 필요한 베스트에포트라, 파서는 코드/이름
    후보 키를 관대히 탐색하고 형식 불일치는 graceful 하게 건너뛴다. 목록엔 웹사이트가
    없어 도메인은 None(enrich 단계로). dry_run 은 베이스 더미.
    """

    name = "sgx"
    registry = "sgx"
    countries = frozenset({"sg", "sgp", "singapore", "싱가포르"})
    list_url = "https://api.sgx.com/securities/v1.1"

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """SGX securities 목록을 1회 조회해 상장사를 수집한다(코드 dedup + 캡)."""
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        listed_seg = Segment(country=segment.country, industry=segment.industry, listed="listed")
        # SGX securities 는 전 종목을 1회 응답으로 준다(페이징 미구현 — 실 응답에 페이징이
        # 있다면 온네트워크 확인 후 추가). 캡은 클라이언트측 len(out)>=cap 으로 적용.
        try:
            payload = fetcher.get_json(self.list_url, params={"excludetypes": "bonds"})
        except Exception as exc:  # WAF/네트워크/형식 → graceful 빈 결과.
            log.info("sgx.error", err_type=type(exc).__name__, err=str(exc))
            return []

        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        for row in _sgx_rows(payload):
            symbol = _first_str(row, ("nc", "code", "symbol"))
            name = _first_str(row, ("n", "name", "companyName"))
            if not symbol or not name or symbol in seen:
                continue
            seen.add(symbol)
            out.append(
                build_company(
                    source=self.name, segment=listed_seg, name=name,
                    domain=None, registry=self.registry, registry_id=symbol,
                )
            )
            if len(out) >= cap:
                break
        log.info("sgx.live", segment=segment.label, n=len(out))
        return out


class IdxSource(ExchangeSource):
    """인도네시아 거래소(IDX) 상장목록 소스 — GetCompanyProfiles JSON(베스트에포트).

    IDX 는 ``idx.co.id/primary/ListedCompany/GetCompanyProfiles`` 로 상장사 프로필을
    페이징 제공한다. 실서비스는 Cloudflare 보호로 정적 접근이 막힐 수 있어(그 경우 graceful
    빈 결과) 온네트워크 확인이 필요한 베스트에포트다. 목록엔 웹사이트가 없어 도메인 None.
    dry_run 은 베이스 더미.
    """

    name = "idx"
    registry = "idx"
    countries = frozenset({"id", "idn", "indonesia", "인도네시아"})
    list_url = "https://www.idx.co.id/primary/ListedCompany/GetCompanyProfiles"
    _MAX_PAGES = 20

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """IDX GetCompanyProfiles 를 페이지네이션하며 상장사를 수집한다(코드 dedup + 캡)."""
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        listed_seg = Segment(country=segment.country, industry=segment.industry, listed="listed")
        page_size = min(100, cap)

        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        start = 0
        page = 0
        while len(out) < cap and page < self._MAX_PAGES:
            try:
                payload = fetcher.get_json(
                    self.list_url,
                    params={"start": start, "length": page_size, "kodeEmiten": ""},
                )
            except Exception as exc:  # Cloudflare/네트워크/형식 → graceful 부분결과.
                log.info("idx.error", start=start, err_type=type(exc).__name__, err=str(exc))
                break
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                break  # 빈/비list(dict 등 예상밖 스키마) → graceful 종료(무한 페이징 방지).
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = _first_str(row, ("KodeEmiten", "code", "symbol"))
                name = _first_str(row, ("NamaEmiten", "name", "companyName"))
                if not symbol or not name or symbol in seen:
                    continue
                seen.add(symbol)
                out.append(
                    build_company(
                        source=self.name, segment=listed_seg, name=name,
                        domain=None, registry=self.registry, registry_id=symbol,
                    )
                )
                if len(out) >= cap:
                    break
            start += len(rows)
            page += 1
        log.info("idx.live", segment=segment.label, n=len(out))
        return out


class BursaSource(ExchangeSource):
    """말레이시아 거래소(Bursa) 상장목록 소스 — 라이브는 검증대기 no-op(dry 전용).

    Bursa 공개 목록은 JS 렌더링/봇 보호로 정적 HTTP 수집이 어렵다(SET 와 동일 상황).
    실연동 전까지 라이브는 네트워크 없이 빈 결과(말레이시아는 Tier A 로 커버).
    TODO(live): 헤드리스 브라우저 또는 공식 다운로드 엔드포인트 확인 필요.
    """

    name = "bursa"
    registry = "bursa"
    countries = frozenset({"my", "mys", "malaysia", "말레이시아"})
    bypass_capable = True  # JS/봇보호 차단 → enable_bypass 시 InsaneFetcher 로 우회.
    list_url = "https://www.bursamalaysia.com/market_information/equities_prices"

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """Bursa 공개 목록은 JS 렌더링/봇 보호로 정적 차단(SET 와 동일 상황).

        ``enable_bypass`` 시 벤더 엔진으로 목록 HTML 을 가져와 파싱, off 면 기존 빈 결과
        (말레이시아는 Tier A 로 커버). 우회 실패/형식불일치도 graceful 빈 결과.
        """
        if not self._settings.enable_bypass:
            log.info("bursa.skip.bypass_off", segment=segment.label)
            return []
        try:
            html = self._client().get_text(self.list_url)
        except Exception as exc:
            log.info("bursa.error", err_type=type(exc).__name__, err=str(exc))
            return []
        rows = self._parse_listing(html, segment)
        log.info("bursa.bypass", segment=segment.label, found=len(rows))
        return rows


def _sgx_rows(payload: Any) -> list[Any]:
    """SGX 응답에서 종목 리스트를 관대히 추출한다(data.prices / data / prices)."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            prices = data.get("prices")
            if isinstance(prices, list):
                return prices
        if isinstance(data, list):
            return data
        prices = payload.get("prices")
        if isinstance(prices, list):
            return prices
    return []


def _first_str(row: Any, keys: tuple[str, ...]) -> str:
    """dict 에서 후보 키 중 첫 비어있지 않은 문자열 값을 반환한다(없으면 빈 문자열)."""
    if not isinstance(row, dict):
        return ""
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""
