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


def test_cap_enforced_under_concurrent_workers() -> None:
    """공유 인스턴스를 여러 워커 스레드가 동시에 resolve() 해도 캡을 초과해 호출하지
    않는다 — ``resolve_batch`` 가 전 워커 공유 단일 인스턴스를 쓰는 근거였던 핵심 주장을
    실측(2026-07-10 적대 리뷰 테스트갭: 순차 ``test_cap_enforced`` 만으론 ``_reserve`` 락의
    동시성 정확성이 검증되지 않았다)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    class _CountingFetcher:
        def __init__(self) -> None:
            self.calls = 0
            self._lock = threading.Lock()

        def get_json(self, url: str, *, params: dict | None = None) -> dict:
            with self._lock:
                self.calls += 1
            return {"items": []}

    cap = 5
    f = _CountingFetcher()
    # search_provider="cse" 로 강제 — 로컬 .env 의 실제 serper 키가 auto 선택을 새치기해
    # (post_json 미구현 _CountingFetcher 가 조용히 예외처리돼 호출이 안 잡히는) 환경별
    # 결과 차이를 없앤다(다른 테스트들의 로컬 베이스라인 실패와 동일 원인, memory 참고).
    r = DomainResolver(_settings(domain_resolve_max=cap, search_provider="cse"), fetcher=f)
    targets = [_dc(f"Company{i}", country="US") for i in range(20)]

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(r.resolve, targets))

    assert f.calls == cap  # 동시 20건 중 정확히 캡만큼만 실제 fetch_page 호출.


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


def _no_naver_settings(**over) -> Settings:
    # KR 은 naver 키가 있으면 자동으로 네이버 라우팅된다 — 로컬 .env 라이브 키(3앱 로테이션,
    # #236)가 새치기하지 못하게 1~3번 앱 전부 명시적으로 비워 CSE(FakeFetcher)로 고정한다
    # (다른 KR 테스트들의 로컬 베이스라인 오염과 동일 원인, memory 참고).
    return _settings(
        naver_client_id="", naver_client_secret="",
        naver_client_id_2="", naver_client_secret_2="",
        naver_client_id_3="", naver_client_secret_3="",
        search_provider="cse",  # .env 라이브 serper 키가 auto 선택에 새치기 못 하게 고정.
        **over,
    )


def test_korean_name_without_name_eng_still_searches_via_title_match() -> None:
    """영문명 없는 순수 한글 상호명(NPS 등)도 이제 검색은 한다 — title 에 상호명이 없으면 미해석.

    구 동작(호출 자체를 안 함)은 2026-07-13 4만건 발견/저장 115건 사고의 근본원인이었다:
    name_eng 를 안 주는 소스(NPS)가 슬러그 매칭 불가로 전량 스킵돼 도메인을 못 채웠다.
    """
    f = FakeFetcher(_items("https://skhynix.com"))  # title 없음 → "" 취급, 매칭 실패.
    r = DomainResolver(_no_naver_settings(), fetcher=f)
    dc = DiscoveredCompany(canonical_key="reg:dart:2", name="에스케이하이닉스", country="KR")
    assert r.resolve(dc) is None
    assert f.calls == 1  # 더 이상 무조건 스킵하지 않음.


def test_korean_name_resolves_via_title_match() -> None:
    """name_eng 없어도 검색결과 title 에 정규화 상호명이 그대로 있으면 채택."""
    f = FakeFetcher(
        {"items": [{"link": "https://skhynix.com", "title": "에스케이하이닉스 공식 홈페이지"}]}
    )
    r = DomainResolver(_no_naver_settings(), fetcher=f)
    dc = DiscoveredCompany(canonical_key="reg:dart:3", name="에스케이하이닉스", country="KR")
    assert r.resolve(dc) == "skhynix.com"


def test_korean_name_title_mismatch_rejected() -> None:
    """title 에 상호명이 없는 엉뚱한 결과는 도메인만 그럴듯해도 채택하지 않는다(정밀도 우선)."""
    f = FakeFetcher(
        {"items": [{"link": "https://skhynix.com", "title": "반도체 뉴스 - 전자신문"}]}
    )
    r = DomainResolver(_no_naver_settings(), fetcher=f)
    dc = DiscoveredCompany(canonical_key="reg:dart:4", name="에스케이하이닉스", country="KR")
    assert r.resolve(dc) is None


def test_naver_b_tags_stripped_before_match() -> None:
    """네이버 title 의 <b> 하이라이트가 매칭 전에 제거된다(#239 리뷰 HIGH #2).

    태그를 안 지우면 정규화 시 'b' 가 상호명 사이에 끼어 정확한 공식 사이트가 탈락한다.
    """
    f = FakeFetcher(
        {"items": [{"link": "https://skhynix.com",
                    "title": "에스케이<b>하이닉스</b> 반도체 공식"}]}
    )
    r = DomainResolver(_no_naver_settings(), fetcher=f)
    dc = DiscoveredCompany(canonical_key="reg:dart:5", name="에스케이하이닉스", country="KR")
    assert r.resolve(dc) == "skhynix.com"


def test_short_korean_prefix_not_matched_without_llm() -> None:
    """LLM-off 한글 경로는 토큰 완전일치만 채택 — 접두 오탐(한국전력→한국전력기술) 차단(HIGH #1)."""
    f = FakeFetcher(
        {"items": [{"link": "https://kepco-eng.com", "title": "한국전력기술 홈페이지"}]}
    )
    r = DomainResolver(_no_naver_settings(), fetcher=f)  # LLM 중재 off(기본).
    dc = DiscoveredCompany(canonical_key="reg:dart:6", name="한국전력", country="KR")
    assert r.resolve(dc) is None  # '한국전력' 토큰이 title 에 독립적으로 없음 → 미채택.


def test_serper_fallback_on_kr_naver_miss() -> None:
    """KR 이 네이버에서 miss 하면 Serper 폴백으로 재시도해 후보를 얻는다(수율 레버 ②)."""
    # 네이버(get_json)=빈 결과, Serper(post_json)=title 매칭되는 후보.
    class _KrFallbackFetcher:
        def __init__(self) -> None:
            self.get_calls = 0
            self.post_calls = 0

        def get_json(self, url, *, params=None, headers=None):
            self.get_calls += 1
            return {"items": []}  # 네이버 miss.

        def post_json(self, url, *, json=None, headers=None):
            self.post_calls += 1
            return {"organic": [{"link": "https://skhynix.com", "title": "에스케이하이닉스 공식"}]}

    f = _KrFallbackFetcher()
    r = DomainResolver(
        _naver_settings(serper_api_key="sk", resolve_serper_fallback=True), fetcher=f
    )
    dc = DiscoveredCompany(canonical_key="reg:dart:7", name="에스케이하이닉스", country="KR")
    assert r.resolve(dc) == "skhynix.com"
    assert f.get_calls == 1 and f.post_calls == 1  # 네이버 miss → Serper 1회.


def test_serper_fallback_off_by_default() -> None:
    """폴백 플래그가 꺼져 있으면 네이버 miss 는 그대로 miss(과금 안 함)."""
    class _KrFetcher:
        def __init__(self) -> None:
            self.post_calls = 0

        def get_json(self, url, *, params=None, headers=None):
            return {"items": []}

        def post_json(self, url, *, json=None, headers=None):
            self.post_calls += 1
            return {"organic": [{"link": "https://skhynix.com", "title": "에스케이하이닉스"}]}

    f = _KrFetcher()
    r = DomainResolver(_naver_settings(serper_api_key="sk"), fetcher=f)  # 폴백 off.
    dc = DiscoveredCompany(canonical_key="reg:dart:8", name="에스케이하이닉스", country="KR")
    assert r.resolve(dc) is None
    assert f.post_calls == 0  # Serper 미호출(과금 0).


def test_llm_arbiter_picks_official() -> None:
    """LLM 중재 on — 후보 중 공식 도메인 index 를 골라 채택(짧은 한글명 구제)."""
    f = FakeFetcher(
        {"items": [
            {"link": "https://news.co.kr", "title": "동양 관련 뉴스"},
            {"link": "https://tongyang.com", "title": "동양 공식"},
        ]}
    )
    r = DomainResolver(_no_naver_settings(resolve_llm_arbiter=True, anthropic_api_key="k"), fetcher=f)
    r._arbitrate = lambda dc, cands: 1  # 2번째 후보(공식)를 고르도록 스텁.
    dc = DiscoveredCompany(canonical_key="reg:dart:9", name="동양", country="KR")
    assert r.resolve(dc) == "tongyang.com"


def test_llm_arbiter_abstains() -> None:
    """LLM 이 기권(-1)하면 채택하지 않는다(제약② — 틀린 채택보다 miss)."""
    f = FakeFetcher(
        {"items": [{"link": "https://random.com", "title": "무관한 페이지"}]}
    )
    r = DomainResolver(_no_naver_settings(resolve_llm_arbiter=True, anthropic_api_key="k"), fetcher=f)
    r._arbitrate = lambda dc, cands: -1
    dc = DiscoveredCompany(canonical_key="reg:dart:10", name="동양", country="KR")
    assert r.resolve(dc) is None


def test_llm_arbiter_cap_enforced() -> None:
    """LLM 중재 캡(resolve_llm_max) 초과분은 중재하지 않고 폴백한다(과금 상한)."""
    f = FakeFetcher(
        {"items": [{"link": "https://a.com", "title": "동양 공식"}]},
        {"items": [{"link": "https://b.com", "title": "삼양 공식"}]},
    )
    r = DomainResolver(
        _no_naver_settings(resolve_llm_arbiter=True, anthropic_api_key="k", resolve_llm_max=1),
        fetcher=f,
    )
    calls = {"n": 0}

    def _stub(dc, cands):
        calls["n"] += 1
        return 0

    r._arbitrate = _stub
    assert r.resolve(DiscoveredCompany(canonical_key="reg:dart:11", name="동양", country="KR")) == "a.com"
    # 2번째는 LLM 캡 초과 → 중재 안 함(폴백: 짧은 이름이라 미채택), _arbitrate 1회만 호출.
    r.resolve(DiscoveredCompany(canonical_key="reg:dart:12", name="삼양", country="KR"))
    assert calls["n"] == 1


def test_parse_index_and_strip_tags() -> None:
    from leadcrawler.sources.domain_resolver import _parse_index, _strip_tags

    assert _strip_tags("에스케이<b>하이닉스</b>") == "에스케이하이닉스"
    assert _parse_index("1", 3) == 1
    assert _parse_index("-1", 3) == -1
    assert _parse_index("5", 3) == -1  # 범위 밖 → 기권.
    assert _parse_index("없음", 3) == -1  # 숫자 없음 → 기권.


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
