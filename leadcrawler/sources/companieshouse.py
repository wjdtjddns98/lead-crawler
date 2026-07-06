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
from .base import (
    DiscoveredCompany,
    Segment,
    SupportsCursorStore,
    build_company,
    is_country,
    join_address,
    opt_str,
)
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import (
    industry_from_uk_sic,
    is_broad_industry,
    matches_prefix,
    uk_sic_codes,
    uk_sic_prefixes,
)

log = get_logger("sources.companies_house")

_GB = {"gb", "uk", "gbr", "united kingdom", "britain", "영국"}
_SEARCH_URL = "https://api.company-information.service.gov.uk/advanced-search/companies"
_PAGE = 100  # 페이지당 size(API 상한은 5000이나 레이트리밋·예산 보호로 보수적 설정).
# 업종 필터가 전량 제외해도 페이지를 무한정 넘기지 않게 하는 절대 상한. 깊은 수집을 위해
# 크게 두되(cap 이 실질 상한), 희소매칭 업종에서 레이트리밋(600 요청/5분)에 걸리지 않게
# 보수적으로 — _PAGE=100 → 최대 2만 후보 스캔(200 요청 < 5분 윈도 한도).
_MAX_PAGES = 200
# advanced-search 의 ES 페이징 윈도: start_index+size ≤ 10,000. 초과 요청은 HTTP 500
# (실측 2026-07-06: start_index 9900→200, 9999→500). 이 경계를 넘겨 페이징하면 커서가
# 10000 에 영구 고착돼 매 런 즉시 에러로 0건이 되는 실사고가 있었다 — 경계 도달을
# '모집단 끝'(빈 페이지)과 동일하게 취급해 0 으로 되감는다.
# ponytail: 윈도 밖(>10k) 심층 수집은 쿼리 분할(설립일 범위 등)이 필요해지면 추가.
_API_MAX_WINDOW = 10_000


class CompaniesHouseSource:
    """Companies House 기반 영국 등록기업 발견 소스."""

    name = "companies_house"

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
                rate_limiters=self._rate_limiters,
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
        # 서버측 SIC 필터: 매핑 업종은 advanced-search 의 sic_codes 파라미터로 **업종별 독립
        # ES 윈도**를 연다 — 무필터 전량 페이징이 공용 10k 윈도를 나눠 쓰다 원장 소진으로
        # 신규 0 이 되던 병목을 우회. 미매핑·broad 는 None → 파라미터 생략(기존 동작).
        # 커서: 세그먼트 라벨 키 그대로. 서버 필터로 모집단이 바뀌어 구커서 위치 의미는
        # 달라지지만, #165 윈도 랩(_API_MAX_WINDOW)+소진 0 리셋이 자연 치유하므로 마이그레이션 불요.
        codes = uk_sic_codes(segment.industry)
        # 미매핑 **구체** 업종(예: '자동차·모빌리티' — _UK_SIC 에 키 없음)은 수집하지 않는다:
        # 등록처가 필터를 못 해 전량 통과되고, 비-broad 라 세그먼트 라벨이 그대로 구분에
        # 실려 **아무 UK 법인이 그 업종으로 오라벨**된다(전체크롤 자동차 70% 편중의 진범).
        # broad('전체' 등)는 계속 수집하되 _candidate 의 code_label 게이트가 실업종만 남긴다.
        if prefixes is None and not is_broad_industry(segment.industry):
            log.info("companies_house.skip.unmapped_industry", industry=segment.industry)
            return []

        out: list[DiscoveredCompany] = []
        # 런 간 커서(딥백필): 지난 런이 멈춘 start_index 부터 이어 페이징한다. _MAX_PAGES
        # 캡(레이트리밋 600요청/5분 보호)은 유지 — 커서 영속으로 다음 런이 이어받으므로
        # 캡을 키울 필요 없이 런을 거듭하며 모집단을 소진한다.
        start = 0
        if self._cursor_store is not None:
            start = max(0, self._cursor_store.get(self.name, segment.label))
            if start + _PAGE > _API_MAX_WINDOW:
                # 지난 런들이 API 윈도 끝에 도달(구코드에선 500 고착) — 재검증 사이클 재개.
                log.info("companies_house.cursor.window_wrap", segment=segment.label, start=start)
                start = 0
        exhausted = False
        page = 0
        while len(out) < cap and page < _MAX_PAGES:
            if start + _PAGE > _API_MAX_WINDOW:
                exhausted = True  # 윈도 끝 — 더 요청하면 500. 모집단 끝(빈 페이지)과 동일 취급.
                break
            params = {
                "company_status": "active",  # 제약 ②: 실존 법인만(서버 필터).
                "size": min(_PAGE, cap),
                "start_index": start,
            }
            if codes:
                params["sic_codes"] = ",".join(codes)  # 서버측 업종 필터(독립 윈도).
            try:
                payload = fetcher.get_json(_SEARCH_URL, params=params, headers=headers)
            except Exception as exc:  # 키오류·API오류·네트워크 → 부분결과 보존 후 중단.
                log.info("companies_house.error", start=start, err=str(exc))
                break
            items = payload.get("items") if isinstance(payload, dict) else None
            if not items:
                exhausted = True  # 빈 페이지 = 모집단 끝 → 커서 0 리셋(재검증 재개).
                break
            for item in items:
                dc = self._candidate(segment, item, prefixes)
                if dc is not None:
                    out.append(dc)
                    if len(out) >= cap:
                        break
            # ponytail: cap 도달로 페이지 중간에서 끊겨도 start 는 페이지 단위로 전진 —
            # 남은 항목(≤1페이지)은 다음 소진 사이클에 재스캔된다(dedup 이 걸러냄).
            start += len(items)
            page += 1
        if self._cursor_store is not None:
            if exhausted:
                log.info("companies_house.cursor.exhausted", segment=segment.label, start=start)
            self._cursor_store.advance(self.name, segment.label, 0 if exhausted else start)
        log.info("companies_house.live", segment=segment.label, n=len(out), start=start)
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
        # broad 검색 시 구분을 대분류로 복원: SIC 목록 중 명확 단일매치가 나오는 첫 코드.
        code_label = next(
            (lbl for c in sic_codes if (lbl := industry_from_uk_sic(c)) is not None), None
        )
        # 게이팅(broad 전용): 도메인 없는 UK 등록처 껍데기 법인이 이름만으로 하위 LLM
        # 블라인드 분류되어 오라벨(자동차 편중)+비용 낭비를 만드는 것을 원천 차단 — broad
        # 수집(prefixes 없음)에선 SIC 가 택소노미 대분류로 명확 매핑되는(=실업종) 회사만
        # 채택한다. 구체 업종 크롤(prefixes 있음)은 이미 SIC 필터를 통과했으므로 영향 없음.
        if prefixes is None and code_label is None:
            return None
        # 풍부필드 — 같은 검색 응답이 이미 주는 값(추가 호출 0): 등기주소.
        roa = item.get("registered_office_address")
        address = region = None
        if isinstance(roa, dict):
            address = join_address(
                roa.get("address_line_1"),
                roa.get("address_line_2"),
                roa.get("locality"),
                roa.get("postal_code"),
            )
            region = opt_str(roa.get("locality"))
        return build_company(
            source=self.name,
            segment=segment,
            name=str(name),
            domain=None,  # 등록처 레코드엔 웹사이트 없음 → enrich 단계에서 보강.
            registry="companies_house",
            registry_id=str(number),
            industry_code_label=code_label,
            address=address,
            region=region,
            # 등록번호를 reg_no 에도 채운다 — GLEIF(registeredAs)·OpenCorporates 로 들어온
            # 같은 영국 법인과 dedup 확정 티어에서 교차 매칭되도록(registry_id 는 소스별 상이).
            reg_no=str(number),
        )
