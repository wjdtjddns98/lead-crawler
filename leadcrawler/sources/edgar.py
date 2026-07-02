"""SEC EDGAR 발견 소스 — 미국 상장(공시)기업.

dry_run: 네트워크 없이 결정적 더미. 라이브: 2-패스
1. ``company_tickers_exchange.json`` 로 전체 (CIK, 회사명, 티커, 거래소) 목록,
2. 각 ``submissions/CIK##########.json`` 으로 SIC·홈페이지(website/investorWebsite) 취득 후
   SIC 접두 매칭으로 거른다(website 가 비는 경우 다수 — 도메인 보강은 enrich 단계).
호출량은 ``discovery_max_per_source`` 로 상한한다. SEC fair-access 정책상 식별 가능한
``edgar_user_agent`` 가 필수이며, 없으면 비활성(no-op).
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..dedup import normalize_domain
from ..logging import get_logger
from .base import (
    DiscoveredCompany,
    Segment,
    SupportsCursorStore,
    advance_cursor,
    build_company,
    cursor_offset,
    is_country,
    join_address,
    opt_str,
)
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import industry_from_sic, matches_prefix, sic_prefixes

log = get_logger("sources.edgar")

_US = {"us", "usa", "united states", "미국"}
_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
# 실제 거래소 상장사만(OTC·등록만 한 기업 제외).
_EXCHANGES = {"nasdaq", "nyse", "cboe", "otc"}
# 업종 필터 시 cap*10 까지 넓게 스캔하되, 후보당 submissions.json 1 콜이 발생하므로(SEC
# 무료 레이트리밋 보호) 절대 상한을 둔다 — 예산가드가 무료 등록처엔 안 걸린다.
_SCAN_LIMIT_ABS = 2000


class EdgarSource:
    """SEC EDGAR 기반 미국 상장기업 발견 소스."""

    name = "edgar"

    def __init__(
        self,
        settings: Settings,
        *,
        count: int = 2,
        fetcher: SupportsFetch | None = None,
        rate_limiters: HostRateLimiters | None = None,
        cursor_store: SupportsCursorStore | None = None,
    ) -> None:
        self._settings = settings
        self._count = count
        self._fetcher = fetcher
        self._rate_limiters = rate_limiters
        self._cursor_store = cursor_store

    def applies_to(self, segment: Segment) -> bool:
        """미국 세그먼트에 적용된다(상장여부는 라이브에서 거래소·SIC 로 정제)."""
        return is_country(segment, _US)

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트에 해당하는 미국 기업 목록을 반환한다."""
        if self._settings.dry_run:
            return self._dry(segment)
        if not self._settings.edgar_user_agent:
            log.info("edgar.skip.no_user_agent")
            return []
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(registry_id 기반 canonical_key)."""
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} EDGAR Corp {i}",
                domain=f"us-edgar{i}.com",
                registry="sec",
                registry_id=f"CIK{i:07d}",
            )
            for i in range(self._count)
        ]

    def _client(self) -> SupportsFetch:
        # 소스 인스턴스당 1개만 생성·재사용(discover 호출마다 클라이언트 누수 방지).
        if self._fetcher is None:
            self._fetcher = Fetcher(
                user_agent=self._settings.edgar_user_agent,
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
                rate_limiters=self._rate_limiters,
            )
        return self._fetcher

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """실 EDGAR 발견(거래소 목록 + submissions SIC/도메인 필터)."""
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        prefixes = sic_prefixes(segment.industry)

        try:
            universe = _parse_tickers_exchange(fetcher.get_json(_TICKERS_URL))
        except Exception as exc:  # 깨진 응답이면 전체 크래시 대신 빈 결과.
            log.info("edgar.universe.error", err=str(exc))
            return []
        scan_limit = cap if prefixes is None else min(cap * 10, _SCAN_LIMIT_ABS)
        # 런 간 커서(딥백필): 지난 런이 멈춘 위치부터 이어 스캔한다(매 런 같은 머리 재방문 방지).
        # 상류 목록(company_tickers_exchange) 순서가 런 간 대체로 안정하다고 가정 — 흔들려도
        # 오염은 없고(제약① dedup) 일부 항목이 다음 소진 사이클까지 늦어질 뿐(self-heal).
        offset = cursor_offset(self._cursor_store, self.name, segment, len(universe))
        scanned = 0

        out: list[DiscoveredCompany] = []
        for cik, name in universe[offset : offset + scan_limit]:
            scanned += 1
            try:
                sub = fetcher.get_json(_SUBMISSIONS_URL.format(cik=f"{cik:010d}"))
            except Exception as exc:  # 개별 실패는 건너뛴다.
                log.info("edgar.submissions.error", cik=cik, err=str(exc))
                continue
            if not isinstance(sub, dict):
                continue
            if not matches_prefix(sub.get("sic"), prefixes):
                continue
            website = sub.get("investorWebsite") or sub.get("website")
            address, region = _business_address(sub.get("addresses"))
            tickers = sub.get("tickers")
            out.append(
                build_company(
                    source=self.name,
                    segment=segment,
                    name=sub.get("name") or name,
                    domain=normalize_domain(website if isinstance(website, str) else None),
                    registry="sec",
                    registry_id=str(cik),
                    # broad 검색 시 구분을 대분류로 복원(SIC). 구체 검색이면 무시된다.
                    industry_code_label=industry_from_sic(sub.get("sic")),
                    # 풍부필드 — 같은 submissions 응답이 이미 주는 값(추가 호출 0).
                    address=address,
                    region=region,
                    phone=opt_str(sub.get("phone")),
                    ir_url=opt_str(sub.get("investorWebsite")),
                    ticker=(
                        opt_str(tickers[0]) if isinstance(tickers, list) and tickers else None
                    ),
                )
            )
            if len(out) >= cap:
                break
        advance_cursor(self._cursor_store, self.name, segment, offset + scanned, len(universe))
        log.info("edgar.live", segment=segment.label, n=len(out), offset=offset, scanned=scanned)
        return out


def _business_address(addresses: Any) -> tuple[str | None, str | None]:
    """submissions.addresses.business → (주소 원문, 지역=주(state) 코드)."""
    if not isinstance(addresses, dict):
        return None, None
    business = addresses.get("business")
    if not isinstance(business, dict):
        return None, None
    address = join_address(
        business.get("street1"),
        business.get("street2"),
        business.get("city"),
        business.get("stateOrCountry"),
        business.get("zipCode"),
    )
    return address, opt_str(business.get("stateOrCountry"))


def _parse_tickers_exchange(payload: Any) -> list[tuple[int, str]]:
    """company_tickers_exchange.json → 거래소 상장 (cik, 회사명) 목록.

    형식: ``{"fields": ["cik","name","ticker","exchange"], "data": [[...], ...]}``.
    """
    if not isinstance(payload, dict):
        return []
    fields = [str(f).lower() for f in payload.get("fields", [])]
    idx = {f: i for i, f in enumerate(fields)}
    ci, ni, ei = idx.get("cik"), idx.get("name"), idx.get("exchange")
    out: list[tuple[int, str]] = []
    for row in payload.get("data", []):
        if ci is None or ci >= len(row):
            continue
        exchange = str(row[ei]).lower() if ei is not None and ei < len(row) else ""
        if exchange and exchange not in _EXCHANGES:
            continue
        try:
            cik = int(row[ci])
        except (TypeError, ValueError):
            continue
        name = str(row[ni]) if ni is not None and ni < len(row) else ""
        out.append((cik, name))
    return out
