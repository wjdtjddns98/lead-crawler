"""검색엔진 발견 소스 — 등록처로 안 잡히는 비상장·중소기업 보강.

dry_run: 네트워크 없이 결정적 더미. 라이브: 검색 공급자(:mod:`search_provider` —
Serper 또는 Google CSE)로 결과 도메인을 기업 후보로 변환(포털/뉴스/SNS blocklist +
eTLD+1 dedup). 주의: Bing Web Search API 는 2025-08 폐기(410)되어 라이브 미지원 — bing
키만 있으면 no-op 로그를 남긴다. 공급자가 없으면(키 무) no-op.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import Settings
from ..cost_ledger import SupportsCostLedger
from ..dedup import normalize_domain
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company
from .countries import resolve_country
from .http import SupportsFetch
from .search_provider import SearchProvider, build_search_provider

log = get_logger("sources.search")

# 국가별 검색 현지화: ISO2 → (Google gl 지역코드, lr 언어제한, 현지어 IR 키워드).
# 검색을 현지 언어·지역으로 편향시켜 비영어권(태국·일본 등) 기업 도메인 적중률을 높인다.
_LOCALE: dict[str, tuple[str, str, str]] = {
    "KR": ("kr", "lang_ko", "기업 공식 홈페이지 IR 투자정보"),
    "US": ("us", "lang_en", "company official website investor relations"),
    "GB": ("uk", "lang_en", "company official website investor relations"),
    "PH": ("ph", "lang_en", "company official website investor relations Philippines"),
    "SG": ("sg", "lang_en", "company official website investor relations Singapore"),
    "MY": ("my", "lang_en", "company official website investor relations Malaysia"),
    "IN": ("in", "lang_en", "company official website investor relations India"),
    "AU": ("au", "lang_en", "company official website investor relations Australia"),
    "CA": ("ca", "lang_en", "company official website investor relations Canada"),
    "HK": ("hk", "lang_en", "company official website investor relations Hong Kong"),
    "TH": ("th", "lang_th", "บริษัท เว็บไซต์ทางการ นักลงทุนสัมพันธ์"),
    "JP": ("jp", "lang_ja", "企業 公式サイト IR 投資家情報"),
    "CN": ("cn", "lang_zh-CN", "公司 官网 投资者关系"),
    "TW": ("tw", "lang_zh-TW", "公司 官方網站 投資人關係"),
    "ID": ("id", "lang_id", "perusahaan situs resmi hubungan investor"),
    "VN": ("vn", "lang_vi", "công ty trang web chính thức quan hệ nhà đầu tư"),
    "DE": ("de", "lang_de", "Unternehmen offizielle Website Investor Relations"),
    "FR": ("fr", "lang_fr", "entreprise site officiel relations investisseurs"),
    "BR": ("br", "lang_pt", "empresa site oficial relações com investidores"),
}
# 미등록 국가 폴백(영어 generic — 지역·언어 무편향).
_DEFAULT_LOCALE = ("", "", "company official website investor relations")

# 검색결과 제목에서 떼어낼 네비/IR 문구(회사명 아님). 정규화(영숫자 소문자) 후 등치 비교.
_TITLE_NOISE = frozenset({
    "investorrelations", "investorrelation", "investors", "investor", "ir",
    "relations", "home", "homepage", "officialwebsite", "officialsite",
    "official", "website", "welcome", "about", "aboutus", "company",
    "companyprofile", "profile", "contact", "contactus", "main", "mainpage",
    "overview", "investorscorner", "corporate",
})
# 제목 구분자: :: | : ： • · — – 그리고 ' - '(공백 하이픈 공백). 회사명 내부 하이픈은 보존.
_TITLE_SEP = re.compile(r"\s*(?:::|[|:：•·—–]|\s-\s)\s*")


def _clean_name(title: str, domain: str) -> str:
    """검색결과 제목을 회사명으로 정제 — 'Investor Relations :: Meritage Homes' → 'Meritage Homes'.

    제목을 구분자로 쪼개 IR/네비 문구 조각(투자정보·홈 등)을 버리고, 남은 조각 중 가장
    그럴듯한(가장 긴) 것을 회사명으로 쓴다. 전부 노이즈면 도메인 root 를 제목화한다.
    """
    parts = [p.strip() for p in _TITLE_SEP.split(title) if p.strip()]
    norm = lambda p: re.sub(r"[^a-z0-9]", "", p.lower())  # noqa: E731
    meaningful = [p for p in parts if norm(p) and norm(p) not in _TITLE_NOISE]
    if meaningful:  # 노이즈 아닌 조각 중 가장 긴 것(=회사명일 확률 최고).
        return max(meaningful, key=len)
    # 제목이 통째로 IR/네비 문구 → 도메인 root 를 제목화(meritagehomes → Meritagehomes).
    root = domain.split(".", 1)[0]
    return root.capitalize() if root else (parts[0] if parts else domain)

# 기업 직도메인이 아닌 노이즈(포털·뉴스·SNS·디렉터리·공공). eTLD+1 기준.
_BLOCKLIST = frozenset({
    "naver.com", "daum.net", "nate.com", "google.com", "youtube.com",
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "tistory.com", "blogspot.com", "wordpress.com", "wikipedia.org",
    "hankyung.com", "mk.co.kr", "chosun.com", "donga.com", "joins.com",
    "korcham.net", "kita.net", "saramin.co.kr", "jobkorea.co.kr",
})


class SearchSource:
    """검색엔진 기반 범용 발견 소스(모든 세그먼트 적용)."""

    name = "search"

    def __init__(
        self,
        settings: Settings,
        *,
        count: int = 2,
        fetcher: SupportsFetch | None = None,
        cost_ledger: SupportsCostLedger | None = None,
    ) -> None:
        self._settings = settings
        self._count = count
        self._fetcher = fetcher
        self._cost_ledger = cost_ledger
        self._provider: SearchProvider | None = None

    def applies_to(self, segment: Segment) -> bool:  # noqa: ARG002 — 전 세그먼트 적용
        """검색 발견은 모든 세그먼트에 적용된다."""
        return True

    def _get_provider(self) -> SearchProvider | None:
        # 지연 생성(dry_run 은 안 만듦). 무키면 None.
        if self._provider is None:
            self._provider = build_search_provider(
                self._settings, fetcher=self._fetcher, cost_ledger=self._cost_ledger
            )
        return self._provider

    def discover(
        self, segment: Segment, *, seen: set[str] | None = None
    ) -> list[DiscoveredCompany]:
        """세그먼트에 해당하는 후보 기업 목록을 반환한다.

        ``seen`` 이 주어지면(정규화 도메인 집합 — 글로벌 dedup 시드: DB+런 누적+세그먼트 내
        무료소스 결과) 이미 본 도메인은 산출에서 빼고, 페이지 신규 비율이 낮으면 다음 페이지를
        더 사지 않는다(유료 검색 과금 절감, 제약 ①). None(기본)이면 기존 동작 그대로.
        """
        if self._settings.dry_run:
            return self._dry(segment)
        provider = self._get_provider()
        if provider is None:
            if self._settings.bing_api_key:
                log.info("search.skip.bing_retired")  # Bing API 폐기(410).
            else:
                log.info("search.skip.no_key")
            return []
        return self._live(segment, provider, seen)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(도메인 기반 canonical_key)."""
        cc = (segment.country or "xx").strip().lower()
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} 서치기업 {i}",
                domain=f"{cc}-search{i}.com",
            )
            for i in range(self._count)
        ]

    def _live(
        self,
        segment: Segment,
        provider: SearchProvider,
        seen: set[str] | None = None,
    ) -> list[DiscoveredCompany]:
        """검색 공급자로 기업 도메인 후보를 수집한다(현지화 + blocklist + dedup + 캡).

        ``seen``(글로벌 정규화 도메인 집합)에 이미 있는 후보는 산출·cap 카운트에서 제외하고,
        한 페이지의 실후보 대비 신규 비율이 ``search_min_new_ratio`` 미만이면 다음 페이지를
        더 사지 않고 페이징을 조기중단한다(중복에 과금 방지, 제약 ①).
        """
        s = self._settings
        cap = min(s.discovery_search_max_per_segment, 100)  # 유료 검색 전용 캡(무료와 분리), 최대 100.
        seen_global = seen if seen is not None else frozenset()
        min_new_ratio = s.search_min_new_ratio
        # 세그먼트 국가에 맞춰 쿼리 언어·검색 지역(gl)·언어제한(lr)을 현지화.
        country = resolve_country(segment.country)
        gl, lr, keyword = _LOCALE.get(country.iso2, _DEFAULT_LOCALE) if country else _DEFAULT_LOCALE
        query = f"{segment.industry} {keyword}"

        out: list[DiscoveredCompany] = []
        seen_local: set[str] = set()  # 같은 쿼리 페이지들 간 도메인 중복 제거.
        start = 1
        while len(out) < cap and start <= provider.max_start:
            items = provider.fetch_page(query, gl=gl, lr=lr, start=start)
            if not items:
                break
            page_candidates = 0  # 이 페이지의 실후보(blocklist/로컬중복 제외).
            page_new = 0  # 그중 글로벌 신규(seen 에 없던) 수.
            for item in items:
                dc = self._candidate(segment, item, seen_local)
                if dc is None:
                    continue
                page_candidates += 1
                if dc.domain in seen_global:
                    continue  # 글로벌(DB·런 누적·세그먼트 내 무료소스) 중복 — 적재 안 함.
                page_new += 1
                out.append(dc)
                if len(out) >= cap:
                    break
            start += provider.page_size
            if len(out) >= cap:
                break  # 캡 도달 = 정상 종료(중복 조기중단 아님 — 로그 오귀속 방지).
            # 중복률 조기중단: 실후보가 있었는데 신규 비율이 임계 미만이면 다음 페이지 과금 중단.
            # 주의(CSE 다페이지): 신규 도메인이 뒤페이지에 몰린 쿼리는 과소수확 가능 — 단일페이지
            # Serper 가 주공급자라 실질 무해(CSE 는 폐기 경로). search_min_new_ratio 로 보수 조절.
            if page_candidates > 0 and (page_new / page_candidates) < min_new_ratio:
                log.info(
                    "search.early_abort.dupe",
                    segment=segment.label,
                    page_candidates=page_candidates,
                    page_new=page_new,
                )
                break
        log.info("search.live", segment=segment.label, n=len(out))
        return out

    def _candidate(
        self, segment: Segment, item: dict[str, Any], seen: set[str]
    ) -> DiscoveredCompany | None:
        """검색 결과 1건을 기업 후보로 변환(노이즈/중복 제거)."""
        if not isinstance(item, dict):
            return None
        domain = normalize_domain(item.get("link") or item.get("displayLink"))
        if not domain or domain in _BLOCKLIST or domain in seen:
            return None
        seen.add(domain)
        title = (item.get("title") or "").strip()
        return build_company(
            source=self.name,
            segment=segment,
            name=_clean_name(title, domain) if title else domain,
            domain=domain,
        )
