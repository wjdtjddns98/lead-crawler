"""회사명 → 공식 도메인 해석 — 발견 소스가 도메인을 못 준 기업(GLEIF 등) 보강.

GLEIF·일부 등록처는 법인명만 주고 웹사이트를 안 준다(`domain=None`). 도메인이 없으면
enrich 가 즉시 빈손으로 끝나 사이트·이메일을 못 얻는다. 이 모듈은 회사명+국가로 검색
공급자(:mod:`search_provider` — Serper/CSE)를 질의해 **가장 그럴듯한 공식 도메인 1건**을
고른다. 못 찾으면 None.

설계 원칙 — **정밀도 우선**:
- 회사명 토큰이 도메인 등록 root 와 실제로 겹칠 때만 채택한다. 어설픈 1순위 도메인을
  무조건 받으면 틀린 회사 사이트를 저장해 오염되므로(제약 ②), 불확실하면 None 을 낸다.
- opt-in(`resolve_domains`) + 검색 공급자 필요. 무키·dry_run 은 no-op(결정적 유지).
- 비용: 런당 캡(`domain_resolve_max`)으로 호출·과금(Serper)을 보호, 초과는 로그.
- blocklist(포털·뉴스·SNS)는 SearchSource 와 공유해 단일 출처로 둔다.
"""

from __future__ import annotations

import re

from ..config import Settings
from ..cost_ledger import SupportsCostLedger
from ..dedup import normalize_domain
from ..logging import get_logger
from .base import DiscoveredCompany
from .countries import resolve_country
from .http import SupportsFetch
from .search import _BLOCKLIST, _DEFAULT_LOCALE, _LOCALE
from .search_provider import SearchProvider, build_search_provider

log = get_logger("sources.domain_resolver")

# 회사명에서 떼어낼 법인격·일반어(도메인 매칭 신호로 무의미). 소문자 토큰 기준.
_NAME_STOPWORDS = frozenset({
    "group", "holdings", "holding", "co", "ltd", "inc", "corp", "corporation",
    "company", "limited", "plc", "llc", "lp", "the", "and", "of", "sa", "ag",
    "gmbh", "bhd", "pte", "pcl", "tbk", "co.,ltd", "incorporated", "enterprise",
    "enterprises", "international", "global", "industries", "industry",
})


class DomainResolver:
    """회사명으로 공식 도메인을 해석한다(검색 공급자, 정밀도 우선, dry_run no-op)."""

    def __init__(
        self,
        settings: Settings,
        *,
        fetcher: SupportsFetch | None = None,
        cost_ledger: SupportsCostLedger | None = None,
    ) -> None:
        self._settings = settings
        self._fetcher = fetcher
        self._cost_ledger = cost_ledger
        self._provider: SearchProvider | None = None
        self._used = 0  # 런당 해석 호출 수(quota·과금 캡 추적).
        self._capped_logged = False

    def _get_provider(self) -> SearchProvider | None:
        if self._provider is None:
            self._provider = build_search_provider(
                self._settings, fetcher=self._fetcher, cost_ledger=self._cost_ledger
            )
        return self._provider

    def resolve(self, dc: DiscoveredCompany) -> str | None:
        """발견 기업의 공식 도메인을 해석한다(못 찾으면 None).

        dry_run·무키·캡 초과·이미 도메인 보유면 호출하지 않는다(no-op). 한 기업당 검색
        1쿼리만 써 quota·과금을 아낀다.
        """
        s = self._settings
        if s.dry_run or dc.domain:
            return None
        provider = self._get_provider()
        if provider is None:  # 무키(공급자 없음) → no-op.
            return None
        if self._used >= max(0, s.domain_resolve_max):
            if not self._capped_logged:  # 캡 도달은 한 번만 로그(조용한 누락 방지).
                log.info("resolve.capped", cap=s.domain_resolve_max)
                self._capped_logged = True
            return None

        slug = _name_slug(dc.name)
        if len(slug) < 3:  # 1~2자·비라틴 명칭은 매칭 신뢰도가 낮아 시도하지 않음(quota 절약).
            return None

        country = resolve_country(dc.country)
        gl, lr, keyword = _LOCALE.get(country.iso2, _DEFAULT_LOCALE) if country else _DEFAULT_LOCALE
        # 회사명 + 국가 현지화 키워드(공식/IR). 정확구문 인용("...")은 쓰지 않는다 — 법인명
        # 전체를 따옴표로 묶으면(예: "EMCOR Group, Inc.") 구글이 정확일치만 찾아 organic 0건이
        # 되는 경우가 많다(라이브 확인). 정밀도는 아래 _name_matches(슬러그↔도메인 root) 가
        # 보장하므로 쿼리는 넓게 두고 후보를 매칭 단계에서 거른다.
        query = f"{dc.name} {keyword}"

        self._used += 1
        items = provider.fetch_page(query, gl=gl, lr=lr, start=1)  # 단일 쿼리(기업당 1회).

        tld = f".{country.iso2.lower()}" if country else ""
        best: str | None = None
        for item in items:
            if not isinstance(item, dict):
                continue
            domain = normalize_domain(item.get("link") or item.get("displayLink"))
            if not domain or domain in _BLOCKLIST:
                continue
            if not _name_matches(slug, domain):
                continue
            # 첫 일치(=최상위 관련도)를 기본 채택하되, 국가 TLD 일치 도메인이 있으면 우선.
            if best is None:
                best = domain
            if tld and domain.endswith(tld):
                best = domain
                break
        if best is not None:
            log.info("resolve.hit", name=dc.name, domain=best)
        else:
            log.info("resolve.miss", name=dc.name)
        return best


def _name_slug(name: str) -> str:
    """회사명을 매칭용 슬러그(법인격/일반어 제외, 영숫자만 연결)로 만든다.

    예: 'SK hynix Inc.' → 'skhynix'(inc 제외), 'Doosan Group' → 'doosan'. 비라틴 명칭은
    영숫자가 없어 ''. 짧은 단어(sk·hd)도 보존해 결합형 브랜드(skhynix)와 맞춘다.
    """
    raw = re.split(r"[^a-z0-9]+", name.lower())
    return "".join(t for t in raw if t and t not in _NAME_STOPWORDS)


def _name_matches(slug: str, domain: str) -> bool:
    """도메인 등록 root(첫 라벨)가 회사명 슬러그와 **경계 정합**하는지(정밀도 게이트).

    임의 부분문자열 매칭은 오탐(예: 'sony'→'unisonybank', 'abc'→'abcnews')을 내므로,
    완전일치 또는 한쪽이 다른 쪽의 **접두**일 때만 인정한다(짧은 슬러그/브랜드엔 길이
    하한을 둬 'abc'→'abcnews' 류를 차단). 애매하면 False(미해석)로 두는 정밀도 우선.
    """
    brand = re.sub(r"[^a-z0-9]", "", domain.split(".", 1)[0].lower())  # 첫 라벨, 영숫자만.
    if len(brand) < 3 or len(slug) < 3:
        return False
    if brand == slug:  # 정확 일치(doosan↔doosan, skhynix↔skhynix).
        return True
    # 접두 일치 — 긴 쪽이 짧은 쪽으로 시작할 때만(중간 substring 오탐 차단). 길이 하한 4.
    if len(slug) >= 4 and brand.startswith(slug):  # 'lotte'→'lottegroup'
        return True
    if len(brand) >= 4 and slug.startswith(brand):  # 'samsung'(brand)←'samsungelectronics'
        return True
    return False
