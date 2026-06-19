"""거래소 상장목록 발견 소스(Tier B) — 국가별 상장기업의 권위·유한 소스.

각 증권거래소는 전체 상장사 목록을 공개한다(나라당 수백~수천 건, 유한). 등록처
(EDGAR/DART)와 동일하게 ``reg:<exchange>:<symbol>`` 키로 신뢰도 높게 잡힌다.
산출물의 상장여부는 항상 ``listed``(상장목록이므로). 업종 필터는 하지 않는다
(거래소 목록은 전 섹터 — 업종 정제는 다운스트림).

dry_run: 네트워크 없는 결정적 더미. 라이브: 거래소 공개 목록 엔드포인트를 받아
(symbol, name[, website]) 로 파싱한다. **라이브 엔드포인트/응답 형식은 공개 자료 기반
가정이며, 실연동 시 검증이 필요하다**(dry_run 계약은 완전 동작·테스트됨). 키 불필요.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..dedup import normalize_domain
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company, is_country
from .http import Fetcher, SupportsFetch

log = get_logger("sources.exchanges")


class ExchangeSource:
    """거래소 상장목록 발견 소스의 공통 베이스(서브클래스가 국가·엔드포인트·파싱 제공)."""

    name: str = ""
    registry: str = ""
    countries: frozenset[str] = frozenset()
    list_url: str = ""

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
        """실 거래소 목록 발견(symbol dedup + 캡). 산출 상장여부는 listed 고정."""
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        try:
            payload = fetcher.get_json(self.list_url)
        except Exception as exc:  # 엔드포인트/응답 이상 → 빈 결과(배치 보호).
            log.info(f"{self.name}.error", err=str(exc))
            return []

        listed_seg = Segment(country=segment.country, industry=segment.industry, listed="listed")
        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        for rec in self._records(payload):
            symbol, name, website = self._fields(rec)
            if not symbol or not name or symbol in seen:
                continue
            seen.add(symbol)
            out.append(
                build_company(
                    source=self.name,
                    segment=listed_seg,
                    name=name,
                    domain=normalize_domain(website),
                    registry=self.registry,
                    registry_id=symbol,
                )
            )
            if len(out) >= cap:
                break
        log.info(f"{self.name}.live", segment=segment.label, n=len(out))
        return out

    def _records(self, payload: Any) -> list[Any]:
        """응답에서 상장사 레코드 리스트를 안전 추출한다(서브클래스가 형식별 구현)."""
        raise NotImplementedError

    def _fields(self, rec: Any) -> tuple[str | None, str | None, str | None]:
        """레코드 1건에서 (symbol, name, website) 추출(서브클래스가 형식별 구현)."""
        raise NotImplementedError


def _str(value: Any) -> str | None:
    """JSON 셀을 안전 문자열화(빈/비문자는 None)."""
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


class PseSource(ExchangeSource):
    """필리핀 증권거래소(PSE) 상장목록 소스."""

    name = "pse"
    registry = "pse"
    countries = frozenset({"ph", "phl", "philippines", "필리핀"})
    # TODO(live): PSE Edge 는 통상 POST 폼 엔드포인트 — GET-JSON 가정은 실연동 전
    # 검증 필요(HTTP 메서드·응답형식). 레코드 배열(또는 {"records":[...]}) 형태로 가정.
    list_url = "https://edge.pse.com.ph/companyDirectory/search.ax"

    def _records(self, payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("records", "data", "companies"):
                val = payload.get(key)
                if isinstance(val, list):
                    return val
        return []

    def _fields(self, rec: Any) -> tuple[str | None, str | None, str | None]:
        if not isinstance(rec, dict):
            return None, None, None
        symbol = _str(rec.get("symbol") or rec.get("securitySymbol") or rec.get("stockSymbol"))
        name = _str(rec.get("companyName") or rec.get("name") or rec.get("securityName"))
        website = _str(rec.get("website") or rec.get("companyUrl"))
        return symbol, name, website


class SetSource(ExchangeSource):
    """태국 증권거래소(SET) 상장목록 소스."""

    name = "set"
    registry = "set"
    countries = frozenset({"th", "tha", "thailand", "태국"})
    # TODO(live): SET 공개 데이터는 헤더/POST 요구 가능 — 실연동 전 검증 필요.
    # {"securitySymbols":[...]} 또는 레코드 배열로 가정.
    list_url = "https://www.set.or.th/api/set/stock/list"

    def _records(self, payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("securitySymbols", "stocks", "data", "records"):
                val = payload.get(key)
                if isinstance(val, list):
                    return val
        return []

    def _fields(self, rec: Any) -> tuple[str | None, str | None, str | None]:
        if not isinstance(rec, dict):
            return None, None, None
        symbol = _str(rec.get("symbol") or rec.get("securitySymbol"))
        name = _str(rec.get("nameEN") or rec.get("name") or rec.get("companyName"))
        website = _str(rec.get("website") or rec.get("url"))
        return symbol, name, website
