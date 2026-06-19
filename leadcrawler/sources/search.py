"""검색엔진 발견 소스 — 등록처로 안 잡히는 비상장·중소기업 보강.

dry_run: 네트워크 없이 결정적 더미. 라이브: Google Programmable Search(JSON API)
결과 도메인을 기업 후보로 변환(포털/뉴스/SNS blocklist + eTLD+1 dedup).
주의: Bing Web Search API 는 2025-08 폐기(410)되어 라이브 미지원 — bing 키만 있으면
no-op 로그를 남긴다. 모던 SERP(Serper 등) 연동은 후속 훅(TODO).
``google_cse_key``+``google_cse_cx`` 가 없으면 비활성(no-op).
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..dedup import normalize_domain
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company
from .countries import resolve_country
from .http import Fetcher, SupportsFetch

log = get_logger("sources.search")

_CSE_URL = "https://customsearch.googleapis.com/customsearch/v1"
_PAGE = 10  # CSE 는 호출당 최대 10건.
_MAX_START = 101 - _PAGE  # CSE 는 start+num<=101(최대 100건) 만 허용.

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
    ) -> None:
        self._settings = settings
        self._count = count
        self._fetcher = fetcher

    def applies_to(self, segment: Segment) -> bool:  # noqa: ARG002 — 전 세그먼트 적용
        """검색 발견은 모든 세그먼트에 적용된다."""
        return True

    def _has_cse(self) -> bool:
        s = self._settings
        return bool(s.google_cse_key and s.google_cse_cx)

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트에 해당하는 후보 기업 목록을 반환한다."""
        if self._settings.dry_run:
            return self._dry(segment)
        if not self._has_cse():
            if self._settings.bing_api_key:
                log.info("search.skip.bing_retired")  # Bing API 폐기(410).
            else:
                log.info("search.skip.no_key")
            return []
        return self._live(segment)

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

    def _client(self) -> SupportsFetch:
        # 소스 인스턴스당 1개만 생성·재사용(discover 호출마다 클라이언트 누수 방지).
        if self._fetcher is None:
            self._fetcher = Fetcher(
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
            )
        return self._fetcher

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """Google CSE 로 기업 도메인 후보를 수집한다(현지화 + blocklist + dedup + 캡)."""
        fetcher = self._client()
        s = self._settings
        cap = min(s.discovery_max_per_source, 100)  # CSE 는 쿼리당 최대 100건.
        # 세그먼트 국가에 맞춰 쿼리 언어·검색 지역(gl)·언어제한(lr)을 현지화.
        country = resolve_country(segment.country)
        gl, lr, keyword = _LOCALE.get(country.iso2, _DEFAULT_LOCALE) if country else _DEFAULT_LOCALE
        query = f"{segment.industry} {keyword}"

        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        start = 1
        while len(out) < cap and start <= _MAX_START:
            params = {
                "key": s.google_cse_key,
                "cx": s.google_cse_cx,
                "q": query,
                "num": _PAGE,
                "start": start,
            }
            if gl:
                params["gl"] = gl
            if lr:
                params["lr"] = lr
            try:
                payload = fetcher.get_json(_CSE_URL, params=params)
            except Exception as exc:
                log.info("search.cse.error", start=start, err=str(exc))
                break
            items = payload.get("items") or []
            if not items:
                break
            for item in items:
                dc = self._candidate(segment, item, seen)
                if dc is not None:
                    out.append(dc)
                    if len(out) >= cap:
                        break
            start += _PAGE
        log.info("search.live", segment=segment.label, n=len(out))
        return out

    def _candidate(
        self, segment: Segment, item: dict[str, Any], seen: set[str]
    ) -> DiscoveredCompany | None:
        """CSE 결과 1건을 기업 후보로 변환(노이즈/중복 제거)."""
        if not isinstance(item, dict):
            return None
        domain = normalize_domain(item.get("link") or item.get("displayLink"))
        if not domain or domain in _BLOCKLIST or domain in seen:
            return None
        seen.add(domain)
        return build_company(
            source=self.name,
            segment=segment,
            name=(item.get("title") or domain).strip(),
            domain=domain,
        )
