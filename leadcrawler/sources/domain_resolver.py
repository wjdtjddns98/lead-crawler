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

수율 사다리(저장 실존율 레버 — 무도메인·한글명 기업 구제, 전부 opt-in·캡·예산가드):
① KR=네이버(무료) 1차, 그 외=글로벌 SERP. ② `resolve_serper_fallback`=KR miss 시
유료 Serper 재시도. ③ `resolve_llm_arbiter`=후보를 Claude Agent SDK(구독 인증·무 API키)로
공식 도메인 1건 중재(기권 -1). 짧은 한글명 substring 오탐은 LLM 이 없으면 토큰 완전일치로만
보수 채택(제약②).
"""

from __future__ import annotations

import re
import threading
from typing import NamedTuple

from ..config import Settings
from ..cost_ledger import SupportsCostLedger
from ..dedup import normalize_domain, normalize_name, tokenize_name
from ..logging import get_logger
from .base import DiscoveredCompany
from .countries import resolve_country
from .http import HostRateLimiters, SupportsFetch
from .search import _BLOCKLIST, _DEFAULT_LOCALE, _LOCALE
from .search_provider import SearchProvider, build_naver_provider, build_search_provider

log = get_logger("sources.domain_resolver")

# 펀드·신탁·유동화 SPC 등 '웹사이트가 있을 수 없는' 비영업 엔티티 판정(고정밀 패턴만).
# GLEIF/EDGAR 딥페이지는 LEI 의무 등록된 펀드가 대량으로 나온다(라이브 2026-07-02:
# 한화/키움 투자신탁·TDF·미국 ETF 연발) — 건당 검색 쿼터(네이버 25k/일·Serper 크레딧)를
# 낭비하므로 쿼리 전에 스킵한다. 오탐 주의: 'TRUST'(Northern Trust)·'TDF'(佛 TDF SAS) 같은
# 실기업 어휘는 제외하고, TDF 는 빈티지 연도가 붙은 형태(TDF2050)만 매칭한다.
_FUND_NAME_RE = re.compile(
    r"투자신탁|증권투자회사|투자목적회사|유동화전문|펀드|"
    r"\bETFs?\b|\bUCITS\b|\bSICAV\b|\bFUNDS?\b|INVESTMENT TRUST|TDF\s*20\d\d",
    re.IGNORECASE,
)


def is_fund_entity(name: str | None) -> bool:
    """이름이 펀드/신탁/유동화 SPC 등 비영업 엔티티로 보이면 True(도메인 해석 무의미)."""
    return bool(name and _FUND_NAME_RE.search(name))


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
        rate_limiters: HostRateLimiters | None = None,
    ) -> None:
        self._settings = settings
        self._fetcher = fetcher
        self._cost_ledger = cost_ledger
        # 공유 인스턴스가 워커 스레드끼리 provider(=Fetcher)까지 공유하면(전 워커 단일
        # DomainResolver, 2026-07-10) 호스트별 페이싱을 이 레지스트리로 합산해야 429 를
        # 막는다(적대 리뷰 MED-3 — 없으면 워커 각자의 min_interval 만으로 openapi.naver.com/
        # google.serper.dev 를 racy 하게 때림).
        self._rate_limiters = rate_limiters
        self._provider: SearchProvider | None = None
        self._naver: SearchProvider | None = None
        self._naver_built = False  # None 이 유효값(키 없음)이라 별도 built 플래그로 캐시.
        self._used = 0  # 런당 해석 호출 수(quota·과금 캡 추적).
        self._capped_logged = False
        # LLM 중재(도메인 후보 → 공식 1건 선택) 런당 캡. 검색 캡(_used)과 별도로 센다 —
        # 검색은 무료(네이버)일 수 있지만 LLM 은 항상 유료라 상한을 따로 둔다.
        self._llm_used = 0
        self._llm_capped_logged = False
        # 캡 체크+증가를 원자화하는 락 — 원 설계(run.py)는 메인스레드 단독 호출이라 무의미
        # 했지만, 백필 소비자가 인스턴스를 워커 스레드끼리 공유하면(캡을 진짜 런당 상한으로
        # 지키기 위해 스레드별로 나누지 않음) 락 없인 경합으로 캡이 새어나간다(2026-07-10
        # 적대 리뷰 MED — 워커별 스레드로컬로 우회했다가 캡이 사실상 무제한이 되던 결함).
        # 실제 네트워크 호출은 락 밖에서 하므로 병렬성은 그대로 유지된다.
        self._lock = threading.Lock()

    def _get_provider(self) -> SearchProvider | None:
        if self._provider is None:
            self._provider = build_search_provider(
                self._settings, fetcher=self._fetcher, cost_ledger=self._cost_ledger,
                rate_limiters=self._rate_limiters,
            )
        return self._provider

    def _get_naver(self) -> SearchProvider | None:
        if not self._naver_built:
            self._naver = build_naver_provider(
                self._settings, fetcher=self._fetcher, rate_limiters=self._rate_limiters
            )
            self._naver_built = True
        return self._naver

    def _reserve(self) -> bool:
        """캡 미만이면 이번 호출을 원자적으로 선점(``_used`` 증가) — 동시 호출 안전."""
        with self._lock:
            if self._used >= max(0, self._settings.domain_resolve_max):
                if not self._capped_logged:  # 캡 도달은 한 번만 로그(조용한 누락 방지).
                    log.info("resolve.capped", cap=self._settings.domain_resolve_max)
                    self._capped_logged = True
                return False
            self._used += 1
            return True

    def resolve(self, dc: DiscoveredCompany) -> str | None:
        """발견 기업의 공식 도메인을 해석한다(못 찾으면 None).

        수율 사다리(전부 opt-in·캡·예산가드, 제약② 정밀도 유지):
        ① 1차 검색 — KR=네이버(무료), 그 외=글로벌 SERP. 후보를 정밀도 게이트로 선택.
        ② Serper 폴백(``resolve_serper_fallback``) — KR 이 네이버에서 miss 하면 유료
           글로벌로 재시도해 후보를 합친다(무료 우선, 필요할 때만 과금).
        ③ LLM 중재(``resolve_llm_arbiter``) — 게이트가 못 고른 후보들을 Claude 가
           공식 도메인 1건으로 중재하거나 기권(-1). 짧은 한글명 substring 오탐을 막으면서
           결합형·약어로 root 가 안 맞는 실기업을 구제한다.

        dry_run·무키·캡 초과·이미 도메인 보유면 검색하지 않는다(no-op).
        """
        s = self._settings
        if s.dry_run or dc.domain:
            return None
        if is_fund_entity(dc.name) or is_fund_entity(dc.name_eng):
            # 펀드/신탁/SPC 는 자체 웹사이트가 없다 — 검색 쿼터를 쓰지 않고 스킵.
            log.info("resolve.skip.fund", name=dc.name)
            return None
        country = resolve_country(dc.country)
        is_kr = bool(country and country.iso2 == "KR")
        primary = self._get_provider()
        # KR 기업은 네이버(무료 25,000쿼리/일)로 1차 라우팅해 유료 SERP 크레딧을 아낀다.
        if is_kr:
            primary = self._get_naver() or primary
        if primary is None:  # 무키(공급자 없음) → no-op.
            return None
        if not self._reserve():
            return None

        search_name = dc.name
        slug = _name_slug(search_name)
        korean_core: str | None = None  # 슬러그 매칭 대신 title 대조·LLM 에 쓸 정규화 한글 상호명.
        if len(slug) < 3 and dc.name_eng:
            # 비라틴(한글 등) 명칭은 슬러그가 비어 스킵되던 경로 — 등록처가 준 영문명으로
            # 폴백하면 검색·도메인 매칭 둘 다 가능해진다(KR 기업 도메인 해석 수율 레버).
            search_name = dc.name_eng
            slug = _name_slug(search_name)
        if len(slug) < 3:
            # name_eng 도 없는 순수 한글 상호명(NPS 등은 name_eng 를 아예 안 줌) — 도메인
            # root 는 대개 로마자라 슬러그 대조가 원천 불가능했다(2026-07-13, 4만건 발견에
            # 저장 115건으로 발각). 한글 원문으로 검색해 title 대조/LLM 중재로 잇는다.
            korean_core = normalize_name(dc.name)
            if len(korean_core) < 2:  # 그래도 너무 짧으면(오탐 위험) 시도하지 않음.
                return None
            search_name = dc.name

        gl, lr, keyword = _LOCALE.get(country.iso2, _DEFAULT_LOCALE) if country else _DEFAULT_LOCALE
        # 회사명 + 국가 현지화 키워드(공식/IR). 정확구문 인용("...")은 쓰지 않는다 — 법인명
        # 전체를 따옴표로 묶으면(예: "EMCOR Group, Inc.") 구글이 정확일치만 찾아 organic 0건이
        # 되는 경우가 많다(라이브 확인). 정밀도는 후보 선택 단계에서 거른다.
        query = f"{search_name} {keyword}"
        tld = f".{country.iso2.lower()}" if country else ""

        cands = _candidates_from(primary.fetch_page(query, gl=gl, lr=lr, start=1))
        best = self._pick(dc, cands, slug=slug, korean_core=korean_core, tld=tld, is_kr=is_kr)

        # ② Serper 폴백 — KR 네이버 miss 시 유료 글로벌로 재시도(예산가드는 Serper 내부).
        if best is None and is_kr and s.resolve_serper_fallback:
            fallback = self._get_provider()  # 글로벌(serper/cse) — KR 도 country 무시하고 서비스.
            if fallback is not None and fallback is not primary:
                more = _candidates_from(fallback.fetch_page(query, gl=gl, lr=lr, start=1))
                if more:
                    cands = _merge_candidates(cands, more)
                    best = self._pick(
                        dc, cands, slug=slug, korean_core=korean_core, tld=tld, is_kr=is_kr
                    )

        if best is not None:
            log.info("resolve.hit", name=dc.name, domain=best)
        else:
            log.info("resolve.miss", name=dc.name)
        return best

    def _pick(
        self,
        dc: DiscoveredCompany,
        cands: list[_Candidate],
        *,
        slug: str,
        korean_core: str | None,
        tld: str,
        is_kr: bool,
    ) -> str | None:
        """후보 도메인 중 이 기업의 공식 도메인을 정밀도 우선으로 고른다(없으면 None).

        한글 경로(``korean_core``)는 LLM 중재가 켜졌으면 그쪽에 위임하고, 아니면 보수적
        결정 규칙(title 이 상호명으로 시작 + 충분히 긴 이름)만 자동 채택한다 — 짧은 한글명의
        substring 오탐(예: '동양'→동양생명)을 막는다. 라틴 경로는 도메인 root 대조.

        LLM 중재는 ``is_kr`` 일 때만 탄다(PO 지시 2026-07-13) — 비KR 은 라틴명이 있어
        root 대조로 충분하고, CLI 콜드스타트(~16초/콜)를 글로벌 크롤에 흘리지 않는다.
        """
        if not cands:
            return None
        use_llm = is_kr and self._settings.resolve_llm_arbiter
        if korean_core is not None:
            # 한글 경로는 기사/공고 title 에 상호명이 그대로 들어가 오탐 위험이 높다 —
            # 뉴스/구인 root 후보를 여기서만 제외(라틴 경로는 슬러그 정합이 보호).
            cands = [c for c in cands if not _is_noise_root(c.domain)]
            if not cands:
                return None
            if use_llm:
                return self._llm_pick(dc, cands, korean_core=korean_core, tld=tld)
            return _korean_deterministic_pick(cands, korean_core, tld)
        # 라틴 경로 — 도메인 root ↔ 슬러그 경계 정합.
        best: str | None = None
        for c in cands:
            if not _name_matches(slug, c.domain):
                continue
            if best is None:
                best = c.domain
            if tld and c.domain.endswith(tld):
                return c.domain
        # root 대조 실패분도 LLM 중재로 구제(결합형·약어 실기업) — KR + 켜졌을 때만.
        if best is None and use_llm:
            return self._llm_pick(dc, cands, korean_core=None, tld=tld)
        return best

    def _reserve_llm(self) -> bool:
        """LLM 중재 캡 미만이면 이번 호출을 원자적으로 선점 — 동시 호출 안전."""
        cap = self._settings.resolve_llm_max or self._settings.domain_resolve_max
        with self._lock:
            if self._llm_used >= max(0, cap):
                if not self._llm_capped_logged:
                    log.info("resolve.llm.capped", cap=cap)
                    self._llm_capped_logged = True
                return False
            self._llm_used += 1
            return True

    def _llm_pick(
        self,
        dc: DiscoveredCompany,
        cands: list[_Candidate],
        *,
        korean_core: str | None,
        tld: str,
    ) -> str | None:
        """후보를 Claude 에 주고 공식 도메인 1건을 중재받는다(기권/캡초과/실패 시 폴백).

        캡 초과·anthropic 미설치/인증정보 없음·오류면 한글은 보수적 결정 규칙으로, 라틴은
        None 으로 폴백한다(절대 무근거 채택 금지 — 제약②). 구독 OAuth Bearer 호출이라
        과금은 없다 — cost_ledger 미적재, 예산가드는 무료 레버라 미적용(캡만 제어).
        """
        if not self._reserve_llm():  # 런당 캡 — 서브프로세스 왕복 폭주 방지.
            # 캡 초과는 **miss 로 이월**한다(결정규칙 폴백 금지) — 2026-08-10 사고:
            # 배치당 캡(100)이 즉시 소진되고 나머지 전부가 title 토큰일치 폴백으로
            # 흘러 기업정보 디렉터리(제목에 상호명이 항상 그대로 들어감)를 대량
            # 채택했다. 대상 정렬(last_crawled_at)상 다음 배치가 새 캡으로 재시도한다.
            return None
        shortlist = cands[:_LLM_MAX_CANDIDATES]
        idx, ok = self._arbitrate(dc, shortlist)
        if not ok:
            # 왕복 자체가 실패(claude CLI 미설치/미인증·타임아웃 등) — 캡 슬롯을 환급해
            # 일시 장애가 남은 배치/런의 중재를 영구 불능화하지 않게 한다(적대 리뷰 MED).
            # 폴백으로라도 recall 을 지킨다(한글=토큰일치, 라틴=None).
            self._refund_llm()
            if korean_core is not None:
                return _korean_deterministic_pick(cands, korean_core, tld)
            return None
        if 0 <= idx < len(shortlist):
            return shortlist[idx].domain
        return None  # 모델이 기권(-1) — 무근거 채택 금지(제약②).

    def _refund_llm(self) -> None:
        """왕복 실패로 소모된 LLM 캡 슬롯 1건을 되돌린다(전이 장애가 캡을 영구 소진하지 않게)."""
        with self._lock:
            if self._llm_used > 0:
                self._llm_used -= 1

    def _arbitrate(self, dc: DiscoveredCompany, cands: list[_Candidate]) -> tuple[int, bool]:
        """raw Anthropic API(구독 OAuth)로 후보 index 를 받는다 — (index, ok). 오류 시 (-1, False).

        ``ok`` 는 **실제 API 왕복이 일어났는지**(캡 환급 판별). 미설치/인증정보 없음·호출
        오류는 (−1, False), 응답을 받았으나 index 파싱이 애매하면 (−1, True)=기권(왕복은 일어남).
        """
        lines = "\n".join(
            f"{i}) domain={c.domain} title={c.title!r} snippet={c.snippet[:120]!r}"
            for i, c in enumerate(cands)
        )
        prompt = _ARBITER_PROMPT.format(
            name=dc.name, name_eng=dc.name_eng or "", country=dc.country or "", candidates=lines
        )
        try:
            text = _sdk_complete(prompt, self._settings.resolve_llm_model, self._settings)
        except Exception as exc:  # anthropic 미설치·인증정보 없음·API 오류 → 왕복 없음.
            log.info("resolve.llm.error", err=str(exc))
            return -1, False
        # 왕복 성공 → ok. _parse_index 는 예외-safe(정규식+클램프)라 파싱실패=기권(-1)이지만 ok 유지.
        log.info("resolve.llm.call", name=dc.name, model=self._settings.resolve_llm_model)
        return _parse_index(text, len(cands)), True


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


# ── 후보 수집·선택 헬퍼(수율 3레버 공용) ──────────────────────────────────────

_LLM_MAX_CANDIDATES = 6  # LLM 중재에 보낼 후보 상한(프롬프트 비용·토큰 절약).
_KOREAN_TOKEN_MIN = 3  # LLM-off 한글 결정 채택 최소 상호명 길이(짧으면 오탐 위험 → 포기).
_TAG_RE = re.compile(r"<[^>]+>")  # 네이버 title 의 <b>…</b> 하이라이트 등 제거.

_ARBITER_PROMPT = (
    "회사의 **공식 웹사이트** 도메인을 아래 후보 중에서 고르라. 뉴스·블로그·위키·디렉토리·"
    "쇼핑몰·SNS·채용사이트·다른 회사 사이트는 제외한다. 공식 사이트가 없거나 확신이 없으면 "
    "-1 을 출력하라(틀린 채택보다 기권이 낫다).\n"
    "오직 후보 번호 하나(또는 -1)만 출력하라(설명·문장 금지).\n\n"
    "회사: 이름={name!r} 영문명={name_eng!r} 국가={country!r}\n"
    "후보:\n{candidates}"
)


class _Candidate(NamedTuple):
    """검색결과 1건의 선택용 정규화 뷰 — 도메인 + (태그 제거된) title/snippet."""

    domain: str
    title: str
    snippet: str


# 기업 공식 도메인이 구조적으로 불가능한 KR 2단계 접미(정부·협회/비영리·학교·연구기관).
# blocklist 는 개별 나열이라 항상 뒤처진다 — 부류 전체를 게이트로 막는다(2026-07-13
# 실측: gg.go.kr·gb.go.kr(도청 보도자료)·kpi.or.kr·kati.or.kr(협회)가 홈페이지로 채택됨).
_NON_COMPANY_SUFFIXES = (".go.kr", ".or.kr", ".ac.kr", ".re.kr", ".hs.kr", ".ms.kr", ".es.kr")
# normalize_domain 이 2라벨로 접는 접미(re/hs/ms/es 는 dedup 의 two_level_tlds 에 없어
# 'lab.re.kr'→'re.kr')는 endswith 로 안 잡힌다 — 정규화 산출 등가비교로 부류 전체를 커버
# (교차리뷰 MED: endswith 단독은 4개 접미가 조용히 죽은 코드였다).
_NON_COMPANY_EXACT = frozenset(
    {"go.kr", "or.kr", "ac.kr", "re.kr", "hs.kr", "ms.kr", "es.kr"}
)
# 뉴스·구인 어휘가 등록 root 에 든 도메인 — 기사/공고 title 에 상호명이 그대로 들어가
# **한글 결정 규칙(title 토큰일치)** 이 오탐하는 주 경로(edaily·dailystock·jobplanet·
# findjob24h 실측). 한글(korean_core) 경로에만 적용한다 — 전 국가 후보 필터로 쓰면
# americanexpress('press')·jobyaviation('job')·newscorp 같은 실기업을 원천 차단한다
# (교차리뷰 HIGH — 라틴 경로는 root↔슬러그 경계 정합이 이미 오탐을 막는다).
_NOISE_ROOT_RE = re.compile(r"news|daily|ilbo|press|recruit|job", re.IGNORECASE)


def _is_noise_domain(domain: str) -> bool:
    """전 경로 공통 제외 부류인지(정부/협회/학교/연구기관 — 기업 공식 도메인 불가)."""
    return domain in _NON_COMPANY_EXACT or domain.endswith(_NON_COMPANY_SUFFIXES)


def _is_noise_root(domain: str) -> bool:
    """한글 title-매칭 경로 한정 제외(뉴스/구인 어휘 root) — 라틴 경로엔 적용 금지.

    ponytail: substring 매칭이라 root 에 해당 어휘가 든 희귀 KR 실기업(데일리호텔 류)은
    이 경로에서 miss 된다(오탐 20/60 실측 대비 수용한 트레이드오프 — 리뷰 합의).
    업그레이드 경로: 어휘 단어경계 앵커링 또는 LLM 중재를 필터보다 먼저 태우기.
    """
    return bool(_NOISE_ROOT_RE.search(domain.split(".", 1)[0]))


def _strip_tags(text: str) -> str:
    """검색결과 title/snippet 의 HTML 태그를 제거한다(네이버 <b> 하이라이트 대응).

    태그를 안 지우면 정규화 시 'b' 가 상호명 사이에 끼어(에스케이<b>하이닉스</b> →
    에스케이b하이닉스) 정확한 공식 사이트가 오히려 매칭에서 탈락한다(#239 리뷰 HIGH).
    """
    return _TAG_RE.sub("", text)


def _candidates_from(items: list) -> list[_Candidate]:
    """공급자 raw 결과를 blocklist·중복도메인 제거한 후보 목록으로 정규화한다(순서 보존)."""
    out: list[_Candidate] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        domain = normalize_domain(item.get("link") or item.get("displayLink"))
        if not domain or domain in _BLOCKLIST or domain in seen or _is_noise_domain(domain):
            continue
        seen.add(domain)
        title = _strip_tags(item.get("title") or "")
        snippet = _strip_tags(item.get("snippet") or item.get("description") or "")
        out.append(_Candidate(domain=domain, title=title, snippet=snippet))
    return out


def _merge_candidates(a: list[_Candidate], b: list[_Candidate]) -> list[_Candidate]:
    """b 중 a 에 없는 도메인만 뒤에 붙인다(1차 결과 우선순위 보존)."""
    seen = {c.domain for c in a}
    return a + [c for c in b if c.domain not in seen]


def _korean_deterministic_pick(
    cands: list[_Candidate], korean_core: str, tld: str
) -> str | None:
    """LLM-off 한글 경로 폴백 — title 의 **토큰 하나와 정확히 일치**할 때만 채택(경계 정합).

    substring 이 아닌 토큰 완전일치라, '동양'→동양생명·'한국전력'→한국전력기술 같은
    접두 오탐을 차단한다(제약②). 짧은 상호명은 아예 시도하지 않는다.

    ponytail: 다중어절 상호명(예: '포스코 인터내셔널')은 korean_core 가 토큰을 concat 해
    ('포스코인터내셔널') title 의 개별 토큰과 안 맞아 여기선 미채택(오탐 대신 miss — 안전).
    이런 케이스의 recall 은 LLM 중재 경로가 담당한다(resolve_llm_arbiter).
    """
    if len(korean_core) < _KOREAN_TOKEN_MIN:
        return None
    best: str | None = None
    for c in cands:
        if korean_core not in tokenize_name(c.title):  # 토큰 완전일치(경계 보장).
            continue
        if best is None:
            best = c.domain
        if tld and c.domain.endswith(tld):
            return c.domain
    return best


_ARBITER_SYSTEM = "너는 회사 공식 도메인을 고르는 판정기다. 오직 후보 번호 하나 또는 -1만 출력한다."


# Agent SDK 별칭(haiku|sonnet|opus) → raw API 모델 ID. 전체 ID 는 그대로 통과.
_MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
}


def _sdk_complete(prompt: str, model: str, settings: Settings) -> str:
    """raw Anthropic API(구독 OAuth Bearer 우선)로 단발 완성 텍스트 — 실패는 예외로 위임.

    #240 의 Agent SDK(claude CLI 서브프로세스)는 콜당 ~16초 콜드스타트에 더해 Node 가
    띄우는 손자 프로세스가 콘솔창을 번쩍여(Popen 억제는 직계만 커버) 실사용 불가 판정
    (2026-07-13 실측) → 업종분류기(ClaudeClassifier)와 동일한 raw API 경로로 교체
    (창 0·~1초/콜, 당일 996콜 실증). 인증정보 없음/호출 실패는 예외 → 호출측이
    (-1, False) 폴백. 구독 정액이라 cost_ledger 미적재(#240 과 동일 방침).
    """
    import anthropic

    if settings.anthropic_auth_token:  # OAuth Bearer(구독) 우선 — 업종분류기와 동일 규약.
        client = anthropic.Anthropic(auth_token=settings.anthropic_auth_token, max_retries=8)
    else:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=8)
    msg = client.messages.create(
        model=_MODEL_ALIASES.get(model, model),
        max_tokens=16,
        system=_ARBITER_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()


def _parse_index(text: str, n: int) -> int:
    """LLM 응답에서 후보 index 를 뽑는다 — 범위 밖·파싱실패는 -1(기권)."""
    m = re.search(r"-?\d+", text or "")
    if not m:
        return -1
    try:
        v = int(m.group())
    except ValueError:
        return -1
    return v if 0 <= v < n else -1
