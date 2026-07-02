"""도메인 해석기 테스트 — 회사명→공식 도메인(가짜 CSE 주입, 네트워크 0)."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.sources.base import DiscoveredCompany
from leadcrawler.sources.domain_resolver import DomainResolver, _name_matches, _name_slug


class FakeFetcher:
    """CSE get_json 만 흉내내는 가짜 fetcher(주입용). 호출 수를 센다.

    ``params`` 는 실제 ``SupportsFetch`` 와 동일하게 keyword-only 로 둬 시그니처 드리프트를
    막는다(resolver 가 keyword 로 호출).
    """

    def __init__(self, *payloads: dict) -> None:
        self._payloads = list(payloads)
        self.calls = 0

    def get_json(self, url: str, *, params: dict | None = None) -> dict:
        self.calls += 1
        return self._payloads.pop(0) if self._payloads else {"items": []}


def _settings(**over) -> Settings:
    base = dict(
        dry_run=False,
        resolve_domains=True,
        google_cse_key="k",
        google_cse_cx="cx",
        domain_resolve_max=50,
    )
    base.update(over)
    return Settings(**base)


def _dc(name: str, *, country: str = "KR", domain: str | None = None) -> DiscoveredCompany:
    return DiscoveredCompany(
        canonical_key=f"reg:lei:{name}", name=name, country=country, domain=domain
    )


def _items(*links: str) -> dict:
    return {"items": [{"link": link} for link in links]}


def test_dry_run_noop() -> None:
    f = FakeFetcher(_items("https://skhynix.com"))
    r = DomainResolver(_settings(dry_run=True), fetcher=f)
    assert r.resolve(_dc("SK hynix")) is None
    assert f.calls == 0  # dry_run 은 네트워크 미경유.


def test_no_cse_key_noop() -> None:
    f = FakeFetcher(_items("https://skhynix.com"))
    r = DomainResolver(_settings(google_cse_key=""), fetcher=f)
    assert r.resolve(_dc("SK hynix")) is None
    assert f.calls == 0


def test_already_has_domain_noop() -> None:
    f = FakeFetcher()
    r = DomainResolver(_settings(), fetcher=f)
    assert r.resolve(_dc("SK hynix", domain="skhynix.com")) is None
    assert f.calls == 0  # 이미 도메인 보유 → 해석 불필요.


def test_resolves_official_domain() -> None:
    f = FakeFetcher(_items("https://skhynix.com/en/main"))
    r = DomainResolver(_settings(), fetcher=f)
    assert r.resolve(_dc("SK hynix Inc.")) == "skhynix.com"


def test_precision_rejects_unrelated() -> None:
    # 회사명과 안 겹치는 도메인만 나오면 미해석(None) — 틀린 매칭으로 오염 방지.
    f = FakeFetcher(_items("https://totally-unrelated-portal.com"))
    r = DomainResolver(_settings(), fetcher=f)
    assert r.resolve(_dc("SK hynix")) is None


def test_blocklist_filtered() -> None:
    # 1순위가 SNS(blocklist)면 건너뛰고 다음 일치 도메인 채택.
    f = FakeFetcher(_items("https://linkedin.com/company/skhynix", "https://skhynix.com"))
    r = DomainResolver(_settings(), fetcher=f)
    assert r.resolve(_dc("SK hynix")) == "skhynix.com"


def test_country_tld_preferred() -> None:
    # 일치 도메인이 여럿이면 국가 TLD(.kr) 를 우선.
    f = FakeFetcher(_items("https://skhynix.com", "https://skhynix.kr"))
    r = DomainResolver(_settings(), fetcher=f)
    assert r.resolve(_dc("SK hynix", country="KR")) == "skhynix.kr"


def test_cap_enforced() -> None:
    f = FakeFetcher(_items("https://skhynix.com"), _items("https://doosan.com"))
    r = DomainResolver(_settings(domain_resolve_max=1), fetcher=f)
    assert r.resolve(_dc("SK hynix")) == "skhynix.com"
    assert r.resolve(_dc("Doosan")) is None  # 캡 초과 → 호출 안 함.
    assert f.calls == 1


def test_non_latin_name_no_fetch() -> None:
    # ASCII 토큰이 없으면(비라틴 명칭) 검색 자체를 시도하지 않음.
    f = FakeFetcher(_items("https://example.com"))
    r = DomainResolver(_settings(), fetcher=f)
    assert r.resolve(_dc("주식회사")) is None
    assert f.calls == 0


def test_cse_error_returns_none() -> None:
    class Boom:
        calls = 1

        def get_json(self, url, *, params=None):
            raise RuntimeError("network down")

    r = DomainResolver(_settings(), fetcher=Boom())
    assert r.resolve(_dc("SK hynix")) is None  # 검색 실패는 미해석으로 안전 종료.


def test_precision_rejects_false_positives() -> None:
    # 리뷰 적발 오탐 케이스 — 중간 substring·결합형으로 다른 회사 도메인을 잡으면 안 됨.
    r = DomainResolver(_settings(), fetcher=FakeFetcher(_items("https://unisonybank.com")))
    assert r.resolve(_dc("Sony", country="JP")) is None  # brand 가 'sony' 를 내부 포함.

    r2 = DomainResolver(_settings(), fetcher=FakeFetcher(_items("https://sung.com")))
    assert r2.resolve(_dc("Samsung Electronics")) is None  # 'sung' ⊂ 슬러그지만 접두 아님.

    r3 = DomainResolver(_settings(), fetcher=FakeFetcher(_items("https://abcnews.com")))
    assert r3.resolve(_dc("ABC Holdings")) is None  # 짧은 슬러그 'abc' → 'abcnews' 차단.


def test_brand_prefix_match_accepted() -> None:
    # 정상 접두 매칭 — 'samsung'(brand) ← 'samsungelectronics'(슬러그)는 채택.
    r = DomainResolver(_settings(), fetcher=FakeFetcher(_items("https://samsung.com")))
    assert r.resolve(_dc("Samsung Electronics")) == "samsung.com"
    # 'lotte' → 'lottegroup' 접두도 채택.
    r2 = DomainResolver(_settings(), fetcher=FakeFetcher(_items("https://lottegroup.com")))
    assert r2.resolve(_dc("Lotte")) == "lottegroup.com"


def test_name_slug_strips_legal_words() -> None:
    assert _name_slug("SK hynix Inc.") == "skhynix"  # 'inc' 제외, 'sk' 보존.
    assert _name_slug("Doosan Group Corporation") == "doosan"
    assert _name_slug("주식회사") == ""  # 비라틴 → 빈 슬러그.


def test_name_matches_boundary() -> None:
    assert _name_matches("skhynix", "skhynix.com") is True  # 정확 일치.
    assert _name_matches("doosan", "doosan.com") is True
    assert _name_matches("samsungelectronics", "samsung.com") is True  # brand 접두.
    assert _name_matches("lotte", "lottegroup.com") is True  # 슬러그 접두.
    assert _name_matches("sony", "unisonybank.com") is False  # 중간 substring 차단.
    assert _name_matches("abc", "abcnews.com") is False  # 짧은 슬러그 차단.
    assert _name_matches("hynix", "samsung.com") is False


def test_korean_name_falls_back_to_name_eng() -> None:
    """한글 명칭(비라틴 → 슬러그 불가)은 등록처 영문명으로 폴백해 해석한다."""
    f = FakeFetcher(_items("https://skhynix.com/ko/main"))
    r = DomainResolver(_settings(), fetcher=f)
    dc = DiscoveredCompany(
        canonical_key="reg:dart:1", name="에스케이하이닉스", country="KR",
        name_eng="SK hynix Inc.",
    )
    assert r.resolve(dc) == "skhynix.com"
    assert f.calls == 1


def test_korean_name_without_name_eng_still_skipped() -> None:
    """영문명도 없으면 기존대로 스킵(quota 절약) — 폴백이 무근거 검색을 만들지 않는다."""
    f = FakeFetcher(_items("https://skhynix.com"))
    r = DomainResolver(_settings(), fetcher=f)
    dc = DiscoveredCompany(canonical_key="reg:dart:2", name="에스케이하이닉스", country="KR")
    assert r.resolve(dc) is None
    assert f.calls == 0


# ── 네이버 검색 API 라우팅(KR 전용·무료) ────────────────────────────────────


class UrlFakeFetcher:
    """호출 URL·헤더를 기록하는 가짜 fetcher — 어느 공급자로 라우팅됐는지 검증용."""

    def __init__(self, *payloads: dict) -> None:
        self._payloads = list(payloads)
        self.urls: list[str] = []
        self.headers: list[dict | None] = []

    def get_json(self, url: str, *, params: dict | None = None, headers: dict | None = None) -> dict:
        self.urls.append(url)
        self.headers.append(headers)
        return self._payloads.pop(0) if self._payloads else {"items": []}


def _naver_settings(**over) -> Settings:
    return _settings(naver_client_id="nid", naver_client_secret="nsecret", **over)


def test_kr_routes_to_naver_when_keys_present() -> None:
    f = UrlFakeFetcher(_items("https://skhynix.com/ko/main"))
    r = DomainResolver(_naver_settings(), fetcher=f)
    assert r.resolve(_dc("SK hynix Inc.", country="KR")) == "skhynix.com"
    assert "openapi.naver.com" in f.urls[0]  # KR → 네이버 백엔드.
    assert f.headers[0]["X-Naver-Client-Id"] == "nid"


def test_non_kr_keeps_default_provider() -> None:
    f = UrlFakeFetcher(_items("https://emcorgroup.com"))
    r = DomainResolver(_naver_settings(), fetcher=f)
    assert r.resolve(_dc("EMCOR Group", country="US")) == "emcorgroup.com"
    assert "customsearch.googleapis.com" in f.urls[0]  # 비KR → 기존(CSE) 유지.


def test_kr_without_naver_keys_falls_back_to_default() -> None:
    f = UrlFakeFetcher(_items("https://skhynix.com"))
    r = DomainResolver(_settings(), fetcher=f)  # 네이버 키 없음.
    assert r.resolve(_dc("SK hynix", country="KR")) == "skhynix.com"
    assert "customsearch.googleapis.com" in f.urls[0]


def test_naver_only_keys_resolve_kr_but_not_others() -> None:
    # 유료 SERP 키가 하나도 없어도 네이버 키만으로 KR 은 해석된다(비KR 은 no-op).
    f = UrlFakeFetcher(_items("https://skhynix.com"))
    r = DomainResolver(
        _naver_settings(google_cse_key="", google_cse_cx=""), fetcher=f
    )
    assert r.resolve(_dc("Acme Corp", country="US")) is None  # 공급자 없음.
    assert f.urls == []
    assert r.resolve(_dc("SK hynix", country="KR")) == "skhynix.com"
    assert "openapi.naver.com" in f.urls[0]


def test_naver_api_error_returns_miss() -> None:
    class BoomFetcher:
        def get_json(self, url, *, params=None, headers=None):
            raise RuntimeError("quota exceeded")

    r = DomainResolver(_naver_settings(), fetcher=BoomFetcher())
    assert r.resolve(_dc("SK hynix", country="KR")) is None  # 크래시 없음(miss).


# ── 펀드/신탁/SPC 스킵(검색 쿼터 보호) ──────────────────────────────────────


def test_fund_entities_skip_search() -> None:
    from leadcrawler.sources.domain_resolver import is_fund_entity

    funds = (
        "한화 해외채권 EMP 일반사모 증권 투자신탁 1호(채권-재간접파생형)",
        "키움키워드림다이나믹적격TDF2050증권자투자신탁제1호(H)[주식혼합-재간접형]",
        "케이비제12차유동화전문유한회사",
        "미래에셋글로벌펀드",
        "xETFs AI Bottleneck & Chokepoint ETF",
        "KEY MULTI ALTERNATIVES SOLUTIONS FUND",
        "ABC UCITS ICAV",
        "Scottish Mortgage Investment Trust PLC",
    )
    for name in funds:
        assert is_fund_entity(name), name
    f = UrlFakeFetcher(_items("https://never-called.com"))
    r = DomainResolver(_naver_settings(), fetcher=f)
    dc = DiscoveredCompany(
        canonical_key="reg:lei:F1", name="한화글로벌채권ETF일반사모증권투자신탁12호",
        country="KR", name_eng="Hanwha Global Bond Trust 12",
    )
    assert r.resolve(dc) is None
    assert f.urls == []  # 검색 쿼터 미사용.


def test_real_companies_not_flagged_as_fund() -> None:
    from leadcrawler.sources.domain_resolver import is_fund_entity

    real = (
        "SK hynix Inc.", "Northern Trust Corporation", "TDF SAS",  # 佛 통신인프라 실기업.
        "삼성전자", "두산에너빌리티", "Fundrise LLC", "현대투자파트너스",
    )
    for name in real:
        assert not is_fund_entity(name), name
