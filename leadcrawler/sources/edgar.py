"""SEC EDGAR 발견 소스 — 미국 공시기업(상장+비상장 파일러).

dry_run: 네트워크 없이 결정적 더미. 라이브는 업종 매핑 여부로 모집단이 갈린다:
- 매핑 업종: ``browse-edgar?action=getcompany&SIC=<4자리>`` atom 을 코드별 페이징해 CIK 를
  전수 열거한다(비상장 파일러 포함 — DART #188 과 동형). atom 의 회사명 필드는 서버 버그로
  깨져(ARRAY(0x..), 2026-07-07 실측) CIK 만 취하고, 이름·SIC 재확인·웹사이트는 아래 2번으로.
- broad/미매핑: ``company_tickers_exchange.json`` (거래소 상장 ~1.2만) 기존 경로 유지.
공통 2-패스: 각 ``submissions/CIK##########.json`` 으로 SIC·홈페이지(website/investorWebsite)
취득 후 SIC 접두 매칭으로 거른다(website 가 비는 경우 다수 — 도메인 보강은 enrich 단계).
호출량은 ``discovery_max_per_source`` 로 상한한다. SEC fair-access 정책상 식별 가능한
``edgar_user_agent`` 가 필수이며, 없으면 비활성(no-op).
"""

from __future__ import annotations

import re
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
from .industry import (
    industry_from_sic,
    is_broad_industry,
    matches_prefix,
    sec_sic_codes,
    sic_prefixes,
)

log = get_logger("sources.edgar")

_US = {"us", "usa", "united states", "미국"}
_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
_ATOM_PAGE = 100  # browse-edgar count 파라미터(100 수용, 2026-07-07 실측).
_CIK_RE = re.compile(r"<cik>0*(\d+)</cik>")
# 실제 거래소 상장사만(OTC·등록만 한 기업 제외).
_EXCHANGES = {"nasdaq", "nyse", "cboe", "otc"}
_REAL_EXCHANGES = {"nasdaq", "nyse", "cboe"}
# 업종 필터 시 cap*10 까지 넓게 스캔하되, 후보당 submissions.json 1 콜이 발생하므로(SEC
# 무료 레이트리밋 보호) 절대 상한을 둔다 — 예산가드가 무료 등록처엔 안 걸린다.
# 2000→10000 상향(물량): SEC 공정접근 정책 = 10 req/s. http_request_delay(기본 0.12s)면 ~8.3
# req/s 로 정책 이내. 10000 후보 = 10000 요청 ≈ 10000 × 0.12s = 1200s(20분)/풀스캔으로 딥백필
# 1런에 흡수 가능. scan_limit=min(cap*10, ABS) 이라 cap(기본 500)을 올린 딥백필 런에서 상한 역할.
# SIC 전수 경로의 browse-edgar 페이지 콜은 이 예산 **밖**의 추가분 — 최악(저수율 페이지)에도
# 총요청 ≈ 2×예산 + len(codes)(빈 페이지 종결자)로 유계라 폭주 없음.
_SCAN_LIMIT_ABS = 10000


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
        """실 EDGAR 발견 — 매핑 업종은 SIC 전수 열거, broad 는 거래소 목록 경로."""
        prefixes = sic_prefixes(segment.industry)
        # 미매핑 **구체** 업종은 수집하지 않는다(등록처 3소스 공통 가드): SIC 필터 불가 →
        # 전량 통과 → 비-broad 세그먼트 라벨로 오라벨(전체크롤 GB 자동차 70% 과 동일 결함).
        if prefixes is None and not is_broad_industry(segment.industry):
            log.info("edgar.skip.unmapped_industry", industry=segment.industry)
            return []
        if prefixes is not None:
            return self._live_sic_full(segment, prefixes)
        return self._live_tickers(segment, prefixes)

    def _live_tickers(
        self, segment: Segment, prefixes: tuple[str, ...] | None
    ) -> list[DiscoveredCompany]:
        """거래소 상장 목록(company_tickers_exchange) 경로 — broad/미매핑 업종용."""
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
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
        for cik, name, exchange in universe[offset : offset + scan_limit]:
            scanned += 1
            try:
                sub = fetcher.get_json(_SUBMISSIONS_URL.format(cik=f"{cik:010d}"))
            except Exception as exc:  # 개별 실패는 건너뛴다.
                log.info("edgar.submissions.error", cik=cik, err=str(exc))
                continue
            # 상장여부 세분화: universe 는 거래소 필드가 있는 SEC 공시기업 목록 — 실거래소
            # (nasdaq/nyse/cboe)=listed, 장외(otc)=unlisted, 빈 값=세그먼트 값 유지(미상).
            listed = (
                segment.listed
                if not exchange
                else ("unlisted" if exchange == "otc" else "listed")
            )
            dc = self._company_from_submissions(
                segment,
                cik,
                sub,
                prefixes,
                listed=listed,
                market=exchange.upper() or None,
                name_fallback=name,
            )
            if dc is None:
                continue
            out.append(dc)
            if len(out) >= cap:
                break
        advance_cursor(self._cursor_store, self.name, segment, offset + scanned, len(universe))
        log.info("edgar.live", segment=segment.label, n=len(out), offset=offset, scanned=scanned)
        return out

    def _live_sic_full(
        self, segment: Segment, prefixes: tuple[str, ...]
    ) -> list[DiscoveredCompany]:
        """SIC 코드별 browse-edgar 전수 열거 — 비상장 파일러 포함(매핑 업종 전용).

        코드마다 독립 커서(``<라벨>#sic<code>``)로 페이징을 이어가고, 세그먼트 포인터
        커서(``<라벨>#sicptr``)가 런을 거듭하며 코드를 라운드로빈으로 순회한다 — 소진된
        앞 코드를 매 런 재스캔하다 뒤 코드에 영영 못 가는 고착을 방지. 전 코드가 한 바퀴
        소진되면 포인터 0(전 사이클 재검증 재개).
        """
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        # submissions 콜 예산: 기존 티커 경로의 scan_limit 과 동일한 상한(SEC 레이트 보호).
        budget = min(cap * 10, _SCAN_LIMIT_ABS)
        codes = sec_sic_codes(segment.industry) or ()
        if not codes:  # sic_prefixes 가 있으면 코드도 있어야 정상 — 방어적 폴백.
            return self._live_tickers(segment, prefixes)
        ptr_key = f"{segment.label}#sicptr"
        ptr = 0
        if self._cursor_store is not None:
            ptr = self._cursor_store.get(self.name, ptr_key) % len(codes)
        out: list[DiscoveredCompany] = []
        scanned = 0
        stopped_at: int | None = None  # cap/예산으로 멈춘 코드의 인덱스(다음 런 시작점).
        for i in range(len(codes)):
            idx = (ptr + i) % len(codes)
            code = codes[idx]
            key = f"{segment.label}#sic{code}"
            start = 0
            if self._cursor_store is not None:
                start = max(0, self._cursor_store.get(self.name, key))
            exhausted = False
            while len(out) < cap and scanned < budget:
                try:
                    xml = fetcher.get_text(
                        _BROWSE_URL,
                        params={
                            "action": "getcompany",
                            "SIC": code,
                            "owner": "include",
                            "count": _ATOM_PAGE,
                            "start": start,
                            "output": "atom",
                        },
                    )
                except Exception as exc:  # 개별 코드 실패 → 부분결과 보존, 커서 유지.
                    log.info("edgar.browse.error", code=code, start=start, err=str(exc))
                    break
                ciks = [int(c) for c in _CIK_RE.findall(xml)]
                if not ciks:
                    exhausted = True  # 코드 모집단 끝(빈 페이지) → 0 리셋(재검증 재개).
                    break
                for cik in ciks:
                    if len(out) >= cap or scanned >= budget:
                        break
                    scanned += 1
                    try:
                        sub = fetcher.get_json(_SUBMISSIONS_URL.format(cik=f"{cik:010d}"))
                    except Exception as exc:  # 개별 실패는 건너뛴다.
                        log.info("edgar.submissions.error", cik=cik, err=str(exc))
                        continue
                    listed, market = _listing_from_submissions(sub, segment.listed)
                    dc = self._company_from_submissions(
                        segment, cik, sub, prefixes, listed=listed, market=market
                    )
                    if dc is not None:
                        out.append(dc)
                # ponytail: cap/예산 도달로 페이지 중간에서 끊겨도 start 는 페이지 단위 전진 —
                # 남은 항목(≤1페이지)은 다음 소진 사이클에 재스캔된다(dedup 이 걸러냄).
                start += len(ciks)
            if self._cursor_store is not None:
                if exhausted:
                    log.info("edgar.cursor.exhausted", code=code, segment=segment.label)
                self._cursor_store.advance(self.name, key, 0 if exhausted else start)
            if len(out) >= cap or scanned >= budget:
                stopped_at = idx
                break
        if self._cursor_store is not None:
            # cap/예산으로 멈췄으면 그 코드부터 재개, 한 바퀴 전 코드 소진이면 0 랩.
            self._cursor_store.advance(self.name, ptr_key, 0 if stopped_at is None else stopped_at)
        log.info("edgar.live_sic", segment=segment.label, n=len(out), scanned=scanned)
        return out

    def _company_from_submissions(
        self,
        segment: Segment,
        cik: int,
        sub: Any,
        prefixes: tuple[str, ...] | None,
        *,
        listed: str,
        market: str | None,
        name_fallback: str = "",
    ) -> DiscoveredCompany | None:
        """submissions 응답 1건 → 후보(형식불일치/SIC 불일치는 None)."""
        if not isinstance(sub, dict):
            return None
        if not matches_prefix(sub.get("sic"), prefixes):
            return None
        website = sub.get("investorWebsite") or sub.get("website")
        address, region = _business_address(sub.get("addresses"))
        tickers = sub.get("tickers")
        return build_company(
            source=self.name,
            segment=Segment(country=segment.country, industry=segment.industry, listed=listed),
            name=sub.get("name") or name_fallback,
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
            ticker=(opt_str(tickers[0]) if isinstance(tickers, list) and tickers else None),
            market=market,
        )


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


def _listing_from_submissions(sub: Any, default: str) -> tuple[str, str | None]:
    """submissions 의 tickers/exchanges → (listed, market) 세분화.

    티커 없음=unlisted(비상장 파일러 — SIC 전수 열거의 주 대상), 실거래소=listed,
    OTC 등 그 외=unlisted. 거래소 미상(티커만 있음)은 세그먼트 기본값 유지.
    """
    if not isinstance(sub, dict):
        return default, None
    tickers = sub.get("tickers")
    if not (isinstance(tickers, list) and any(tickers)):
        return "unlisted", None
    exchanges = sub.get("exchanges")
    first = ""
    if isinstance(exchanges, list):
        first = next((str(e) for e in exchanges if e), "")
    if not first:
        return default, None
    return ("listed" if first.lower() in _REAL_EXCHANGES else "unlisted"), first.upper()


def _parse_tickers_exchange(payload: Any) -> list[tuple[int, str, str]]:
    """company_tickers_exchange.json → 거래소 상장 (cik, 회사명, 거래소 소문자) 목록.

    형식: ``{"fields": ["cik","name","ticker","exchange"], "data": [[...], ...]}``.
    거래소는 listed/market 세분화에 쓰인다(빈 문자열=미상).
    """
    if not isinstance(payload, dict):
        return []
    fields = [str(f).lower() for f in payload.get("fields", [])]
    idx = {f: i for i, f in enumerate(fields)}
    ci, ni, ei = idx.get("cik"), idx.get("name"), idx.get("exchange")
    out: list[tuple[int, str, str]] = []
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
        out.append((cik, name, exchange))
    return out
