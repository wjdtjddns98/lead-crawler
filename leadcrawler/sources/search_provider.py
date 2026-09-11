"""검색 공급자 추상화 — CSE/Serper 등 SERP 백엔드를 교체 가능하게 한다.

발견(SearchSource)·도메인해석(DomainResolver)은 이 모듈의 :class:`SearchProvider`
한 겹만 의존한다. Google CSE 가 신규 고객에 닫히면서(403 PERMISSION_DENIED) Serper.dev
같은 모던 SERP 로 무중단 교체하기 위한 이음매다. 공급자는 **정규화된 결과**
(``{"link","title"}`` dict 목록)만 돌려주고, blocklist·dedup·정밀도 게이트는 호출부에
그대로 둔다(단일 책임).

선택 규칙(``search_provider``): ``auto``=serper 키>cse 키 순, ``serper``/``cse`` 강제,
``none``=비활성. dry_run 은 공급자 자체를 안 만든다(호출부가 결정적 더미 반환).

비용: Serper 는 유료(크레딧)라 페이지 1건당 ``cost_ledger.record("serper")`` 로 과금을
월 예산에 편입하고, 예산 초과 시 호출 전에 빈 페이지로 차단한다. CSE 는 무과금.
"""

from __future__ import annotations

import html
import re
import threading
import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import unquote

from ..config import Settings
from ..cost_ledger import SupportsCostLedger
from ..logging import get_logger
from .http import Fetcher, HostRateLimiters, SupportsFetch

log = get_logger("sources.search_provider")

_CSE_URL = "https://customsearch.googleapis.com/customsearch/v1"
_SERPER_URL = "https://google.serper.dev/search"
_NAVER_URL = "https://openapi.naver.com/v1/search/webkr.json"
# 네이버 응답 title/description 은 검색어를 <b>…</b> 로 하이라이트하고 HTML 엔티티
# (&gt;·&quot;·&amp;)를 이스케이프한 채 온다 — 공급자 계약(정규화된 결과)에 맞춰 여기서
# 정화한다(안 하면 발견 회사명·도메인해석 토큰매칭에 태그/엔티티가 그대로 유입).
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def clean_search_text(text: str) -> str:
    """검색결과 텍스트 공용 정화 — HTML 태그 제거 + 엔티티 unescape(+trim).

    네이버(<b> 하이라이트·&gt; 엔티티) 대응이 원출처지만 공급자 불문 무해해 발견·도메인해석
    양쪽이 공유한다(3중 구현 방지 — 교차리뷰 LOW). 태그 제거를 unescape 보다 먼저 하는
    순서는 의도: 역순이면 &amp;gt; 가 &gt; 로 풀린 뒤 태그 정규식이 본문을 오려낼 수 있다.
    """
    return html.unescape(_HTML_TAG_RE.sub("", text)).strip()


class SearchProvider(Protocol):
    """SERP 백엔드 최소 인터페이스 — 호출부가 의존하는 계약."""

    name: str
    page_size: int  # 호출당 결과 수(CSE=10, Serper=100).
    max_start: int  # 페이지네이션 상한(1-base 결과 오프셋).

    def fetch_page(self, query: str, *, gl: str, lr: str, start: int) -> list[dict]:
        """``start``(1-base 결과 오프셋)부터 한 페이지의 raw 결과를 반환한다.

        반환 dict 는 최소 ``link``/``title`` 키를 갖는다(CSE/Serper 공통 정규화).
        결과 없음·오류·예산초과는 빈 리스트(호출부 루프 종료 신호).
        """
        ...


def _lr_to_hl(lr: str) -> str:
    """CSE 언어제한(``lang_ko``)을 Serper 인터페이스 언어(``ko``)로 변환한다."""
    if not lr:
        return ""
    return lr.removeprefix("lang_").lower()


class _BaseProvider:
    """공급자 공통 — 설정·페처·원장 보유, 페처 지연 생성."""

    name: str = ""
    page_size: int = 10
    max_start: int = 91

    def __init__(
        self,
        settings: Settings,
        *,
        fetcher: SupportsFetch | None = None,
        cost_ledger: SupportsCostLedger | None = None,
        rate_limiters: HostRateLimiters | None = None,
    ) -> None:
        self._settings = settings
        self._fetcher_obj = fetcher
        self._cost_ledger = cost_ledger
        self._rate_limiters = rate_limiters

    def _fetcher(self) -> SupportsFetch:
        # 공급자 인스턴스당 1개만 생성·재사용(호출마다 클라이언트 누수 방지).
        if self._fetcher_obj is None:
            self._fetcher_obj = Fetcher(
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
                rate_limiters=self._rate_limiters,
            )
        return self._fetcher_obj


class CseProvider(_BaseProvider):
    """Google Programmable Search(JSON API). 무료 100/일·무과금. 신규 키는 403(닫힘)."""

    name = "cse"
    page_size = 10
    max_start = 101 - 10  # CSE 는 start+num<=101(최대 100건) 만 허용.

    def fetch_page(self, query: str, *, gl: str, lr: str, start: int) -> list[dict]:
        s = self._settings
        params: dict = {
            "key": s.google_cse_key,
            "cx": s.google_cse_cx,
            "q": query,
            "num": self.page_size,
            "start": start,
        }
        if gl:
            params["gl"] = gl
        if lr:
            params["lr"] = lr
        try:
            payload = self._fetcher().get_json(_CSE_URL, params=params)
        except Exception as exc:  # 검색 실패는 빈 페이지(루프 종료)로 안전 종료.
            log.info("search.cse.error", start=start, err=str(exc))
            return []
        items = payload.get("items") if isinstance(payload, dict) else None
        return [it for it in (items or []) if isinstance(it, dict)]


class SerperProvider(_BaseProvider):
    """Serper.dev Google Search API(유료·크레딧). POST JSON, 1쿼리 최대 100건."""

    name = "serper"
    page_size = 100  # 호출당 최대 100건 → 세그먼트당 쿼리 수 최소화(CSE 대비 비용·쿼터 유리).
    max_start = 100  # 캡(≤100)이 1페이지에 들어와 페이지네이션 거의 불필요.
    # 크레딧 소진 latch — Serper 는 크레딧이 바닥나면 400 "Not enough credits" 를 돌려준다
    # (2026-07-27 라이브 실측·402 아님). 소진 후에도 KR 도메인해석 폴백 등이 miss 마다
    # 죽은 API 를 계속 때리던 낭비(호출+로그 스팸)를 첫 감지에서 차단한다.
    # ponytail: 인스턴스 수명 latch — 소스 객체(SearchSource·DomainResolver·AiDirectory)가
    # 각자 provider 를 만들고 연속크롤은 라운드마다 전체 재생성하므로, 소진 재감지 호출은
    # 라운드×소스(×워커)당 1회 남는다(miss 마다 때리던 기존 대비 충분). 충전 후 자연 복귀.
    _credits_exhausted = False

    def fetch_page(self, query: str, *, gl: str, lr: str, start: int) -> list[dict]:
        if self._credits_exhausted or self._budget_blocked():
            return []
        page = (start - 1) // self.page_size + 1  # 1-base 오프셋 → Serper page 번호.
        body: dict = {"q": query, "num": self.page_size}
        if gl:
            body["gl"] = gl
        hl = _lr_to_hl(lr)
        if hl:
            body["hl"] = hl
        if page > 1:
            body["page"] = page
        headers = {"X-API-KEY": self._settings.serper_api_key}
        try:
            payload = self._fetcher().post_json(_SERPER_URL, json=body, headers=headers)
        except Exception as exc:
            if self._is_credits_exhausted(exc):
                # 소진은 재시도가 무의미 — latch 를 걸어 이후 호출을 전부 즉시 빈 페이지로.
                # 호출부(KR 네이버 1차 등 무료 경로)는 그대로 동작하고 낭비 호출만 사라진다.
                # 락 불필요: 단방향 bool(False→True)이라 RMW 경합이 없다(Naver 는 idx+=1 라 락).
                self._credits_exhausted = True
                log.warning("search.serper.credits_exhausted")
                return []
            log.info("search.serper.error", page=page, err=str(exc))
            return []
        self._record()  # 호출 1건 = 1 크레딧.
        organic = payload.get("organic") if isinstance(payload, dict) else None
        return [it for it in (organic or []) if isinstance(it, dict)]

    @staticmethod
    def _is_credits_exhausted(exc: Exception) -> bool:
        """크레딧 소진 응답인가 — 400 "Not enough credits"(실측) 또는 402(표준 결제요구).

        일반 400(잘못된 요청)과 구분하려고 본문 메시지까지 확인한다. 응답 객체가 없는
        예외(네트워크 오류 등)는 소진이 아니다(일시 장애를 영구 차단하면 안 됨).
        """
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        if status == 402:
            return True
        if status != 400:
            return False
        try:
            # str() 강제 + 판정 전체를 try 안에 — 비-httpx 페처가 text 로 bytes 등을 줘도
            # 예외가 새지 않는다(오판 시 latch 안 함이 안전측, 호출부 계약 "에러=빈 페이지" 보존).
            return "not enough credits" in str(resp.text or "").lower()
        except Exception:
            return False

    def _budget_blocked(self) -> bool:
        """예산 가드 — 원장 있고 enforce 켜졌고 월 누계가 예산 이상이면 차단."""
        led = self._cost_ledger
        if led is None or not self._settings.cost_budget_enforce:
            return False
        if led.is_over_budget():
            log.info("search.serper.budget_blocked", budget_krw=self._settings.monthly_budget_krw)
            return True
        return False

    def _record(self) -> None:
        if self._cost_ledger is not None:
            self._cost_ledger.record("serper")


class NaverProvider(_BaseProvider):
    """네이버 검색 API(웹문서 webkr). 앱당 무료 25,000쿼리/일 — KR 기업 도메인 해석용.

    KR 한정 백엔드라 :func:`build_search_provider` 선택 사다리(글로벌 SERP)에는 넣지 않고,
    :func:`build_naver_provider` 로 별도 구성해 호출부(DomainResolver)가 KR 기업에만
    라우팅한다. ``gl``/``lr`` 은 무의미(네이버=한국 웹)라 무시한다. 무과금(원장 기록 없음).

    예비 앱(선택, ``naver_client_id_2``/``_3``)이 있으면 **요청마다 라운드로빈**으로
    자격증명을 분산해 합산 일일 쿼터를 앱 수만큼 늘린다(DART 다중키와 동일 취지,
    2026-07-10 429 실사고 대응). 인스턴스가 여러 스레드에 공유될 수 있어(``resolve_batch``
    가 전 워커 공유 단일 해석기를 씀) 인덱스 증가를 락으로 원자화한다.
    """

    name = "naver"
    page_size = 30  # webkr display 상한.
    max_start = 100  # webkr start 상한.

    def __init__(
        self,
        settings: Settings,
        *,
        creds: list[tuple[str, str]],
        fetcher: SupportsFetch | None = None,
        cost_ledger: SupportsCostLedger | None = None,
        rate_limiters: HostRateLimiters | None = None,
    ) -> None:
        super().__init__(settings, fetcher=fetcher, cost_ledger=cost_ledger, rate_limiters=rate_limiters)
        self._creds = creds
        self._idx = 0
        self._idx_lock = threading.Lock()

    def _next_creds(self) -> tuple[str, str]:
        with self._idx_lock:
            cid, secret = self._creds[self._idx % len(self._creds)]
            self._idx += 1
            return cid, secret

    def fetch_page(self, query: str, *, gl: str, lr: str, start: int) -> list[dict]:
        cid, secret = self._next_creds()
        params = {"query": query, "display": self.page_size, "start": start}
        headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}
        try:
            payload = self._fetcher().get_json(_NAVER_URL, params=params, headers=headers)
        except Exception as exc:  # 검색 실패는 빈 페이지(호출부가 miss 처리) — 이 앱이 쿼터
            # 소진이어도 다음 요청은 라운드로빈으로 다른 앱을 타 자동 회복된다.
            log.info("search.naver.error", start=start, err=str(exc))
            return []
        items = payload.get("items") if isinstance(payload, dict) else None
        return [
            {
                **it,
                "title": clean_search_text(str(it.get("title") or "")),
                "description": clean_search_text(str(it.get("description") or "")),
            }
            for it in (items or [])
            if isinstance(it, dict)
        ]


def build_naver_provider(
    settings: Settings,
    *,
    fetcher: SupportsFetch | None = None,
    rate_limiters: HostRateLimiters | None = None,
) -> SearchProvider | None:
    """네이버 공급자를 만든다 — id/secret 쌍이 갖춰진 앱(1~3개)을 전부 모아 로테이션.

    한 쌍도 안 갖춰졌으면 None(기존 동작·회귀 0).
    """
    pairs = (
        (settings.naver_client_id, settings.naver_client_secret),
        (settings.naver_client_id_2, settings.naver_client_secret_2),
        (settings.naver_client_id_3, settings.naver_client_secret_3),
    )
    creds = [(cid, secret) for cid, secret in pairs if cid and secret]
    if not creds:
        return None
    return NaverProvider(settings, creds=creds, fetcher=fetcher, rate_limiters=rate_limiters)


_DDG_URL = "https://html.duckduckgo.com/html/"
_DDG_RESULT_RE = re.compile(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_CHALLENGE_MARK = "bots use DuckDuckGo"
_DDG_LATCH_AFTER = 3  # 연속 챌린지(세션 교체 후에도) 이 횟수면 인스턴스 래치(Serper 소진 latch 관례).
_DDG_IMPERSONATE = ("safari", "chrome131", "safari_ios")  # 세션 교체 때 순환(firefox 는 실측 202).


# DDG ``kl`` 지역코드 — CSE gl/lr 에서 유도하면 JP(jp-ja)·CN/TW(cn-zh-cn) 처럼 틀린 값이 나와
# (리뷰 MED) 명시 표로 둔다. 없는 국가는 빈 값(전 지역 검색).
_DDG_KL = {
    "us": "us-en", "gb": "uk-en", "jp": "jp-jp", "kr": "kr-kr", "cn": "cn-zh", "tw": "tw-tzh",
    "hk": "hk-tzh", "de": "de-de", "fr": "fr-fr", "au": "au-en", "ca": "ca-en", "sg": "sg-en",
    "in": "in-en", "it": "it-it", "es": "es-es", "nl": "nl-nl", "ch": "ch-de", "se": "se-sv",
    "no": "no-no", "dk": "dk-da", "fi": "fi-fi", "br": "br-pt", "mx": "mx-es", "tr": "tr-tr",
    "nz": "nz-en", "id": "id-id", "my": "my-en", "th": "th-th", "ph": "ph-en", "vn": "vn-vi",
    "pl": "pl-pl", "be": "be-nl", "at": "at-de", "ie": "ie-en", "pt": "pt-pt", "gr": "gr-el",
    "il": "il-he",
}


def _ddg_region(gl: str, lr: str) -> str:  # noqa: ARG001 — lr 은 표에 흡수(시그니처 유지).
    """CSE 식 gl → DDG ``kl``(예: us-en·jp-jp·uk-en). 표에 없으면 빈 값(전 지역)."""
    return _DDG_KL.get((gl or "").strip().lower(), "")


def parse_ddg_html(text: str) -> list[dict]:
    """DDG HTML 결과(``class="result__a"`` 앵커) → ``{"link","title"}`` 목록(광고·중복 제거).

    href 는 직접 URL 또는 ``//duckduckgo.com/l/?uddg=<인코딩 URL>`` 리다이렉트 둘 다 온다.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for href, inner in _DDG_RESULT_RE.findall(text):
        link = html.unescape(href)
        m = re.search(r"[?&]uddg=([^&]+)", link)
        if m:
            link = unquote(m.group(1))
        if not link.startswith("http") or "duckduckgo.com" in link or link in seen:
            continue  # 광고(y.js)·내부 링크·제목/스니펫 중복 앵커.
        seen.add(link)
        out.append({"link": link, "title": clean_search_text(inner)})
    return out


class DdgProvider(_BaseProvider):
    """DuckDuckGo HTML 검색(무료·무키) — curl_cffi 로 Safari TLS 지문을 위장해 호출한다.

    배경(2026-09-11 실측): 일반 httpx 로는 결과 0 이지만 TLS 위장이면 정상 결과. US 상장사
    표본 15 → 답 12·정답 12/12(Yahoo 대조), 100회 연속 1 q/s 에서 챌린지 1회(일시적). 유료
    Serper 전에 타는 글로벌 1차 공급자(``build_free_provider``). 단일 페이지(≈10건)라
    발견 recall 은 Serper(100건)보다 낮다 — 발견 대량 수확이 필요하면 ``search_provider=serper``.
    비공식 경로: 202/챌린지 페이지가 연속 ``_DDG_LATCH_AFTER`` 회면 인스턴스 래치(이후 즉시 빈
    페이지 → 호출부는 유료 폴백으로). 과금 없음 → cost_ledger 미적재.
    ponytail: curl_cffi Session 은 스레드 안전 보장이 없어 요청 자체를 락으로 직렬화한다 —
    어차피 ≈1 q/s 페이싱이라 병렬 이득이 없다. 한도는 IP 공용인데 페이싱·래치는 인스턴스
    단위라 ``discovery_workers>1``(워커별 sources)이면 N배로 때린다 — 기본 1 유지, 올리면
    쿨다운이 더 자주 걸릴 뿐 안전(챌린지 → 쿨다운·래치). 프로세스 공유 리미터는 필요해지면.
    """

    name = "ddg"
    page_size = 10
    max_start = 1  # 단일 페이지 — start>1 은 빈 페이지(호출부 루프 종료).

    def __init__(
        self,
        settings: Settings,
        *,
        fetch_fn: Callable[[str, dict], tuple[int, str]] | None = None,
        **kw: object,
    ) -> None:
        super().__init__(settings, **kw)  # type: ignore[arg-type]
        self._fetch_fn = fetch_fn  # 테스트 주입: (url, params) -> (status, text).
        self._session = None
        self._imp_idx = 0
        self._lock = threading.Lock()
        self._last = 0.0
        self._challenges = 0
        self._latched = False

    def _get(self, url: str, params: dict) -> tuple[int, str]:
        if self._fetch_fn is not None:
            return self._fetch_fn(url, params)
        if self._session is None:
            from curl_cffi import requests as cffi_requests  # bypass extra 의존성.

            imp = _DDG_IMPERSONATE[self._imp_idx % len(_DDG_IMPERSONATE)]
            self._session = cffi_requests.Session(impersonate=imp)
        r = self._session.get(url, params=params, timeout=self._settings.http_timeout)
        return r.status_code, r.text

    def _rotate_session(self) -> None:
        """챌린지는 세션(쿠키) 단위다(2026-09-11 실측: 같은 IP 라도 새 세션이면 즉시 200) —
        세션을 버리고 다음 TLS 지문으로 새로 연다."""
        self.close()
        self._imp_idx += 1

    def fetch_page(self, query: str, *, gl: str, lr: str, start: int) -> list[dict]:
        if start > 1 or self._latched:
            return []
        params: dict = {"q": query}
        kl = _ddg_region(gl, lr)
        if kl:
            params["kl"] = kl
        with self._lock:
            # 실측(2026-09-11): 세션당 ~8건 버스트 후 202 챌린지, IP 쿨다운 ≈30s(10s 부족).
            # 챌린지 → 세션 교체 + 쿨다운(30s×연속횟수) 후 같은 쿼리 1회 재시도. 쿨다운 sleep 은
            # 락 안(전 워커 대기) — DDG 한도는 IP 공용이라 같이 쉬는 게 맞다. 연속 3회면 래치.
            for _attempt in range(2):
                now = time.monotonic()
                slot = max(now, self._last + self._settings.ddg_min_interval)
                self._last = slot
                if slot > now:
                    time.sleep(slot - now)
                try:
                    status, text = self._get(_DDG_URL, params)
                except Exception as exc:
                    log.info("search.ddg.error", err=str(exc)[:120])
                    return []
                if status == 200 and _DDG_CHALLENGE_MARK not in text:
                    self._challenges = 0
                    page = parse_ddg_html(text)
                    if not page:  # 진짜 0건과 마크업 변경(파서 실패)을 로그로 구분 가능하게.
                        log.info("search.ddg.empty", query=query[:60], length=len(text))
                    return page
                self._challenges += 1
                log.info("search.ddg.challenge", status=status, n=self._challenges)
                self._rotate_session()
                if self._challenges >= _DDG_LATCH_AFTER:
                    self._latched = True
                    log.warning("search.ddg.latched")
                    return []
                cooldown = self._settings.ddg_cooldown_s * self._challenges
                if cooldown > 0:
                    log.info("search.ddg.cooldown", seconds=cooldown)
                    time.sleep(cooldown)
            return []

    def close(self) -> None:
        s, self._session = self._session, None
        if s is not None:
            try:
                s.close()
            except Exception:  # noqa: BLE001 — 정리 실패는 무시.
                pass


def build_free_provider(settings: Settings) -> SearchProvider | None:
    """무료 글로벌 1차 공급자(DDG). ``search_free_ddg`` 꺼짐·강제 공급자(serper/cse/none)·
    curl_cffi 미설치면 None(호출부는 유료 공급자로)."""
    choice = (settings.search_provider or "auto").strip().lower()
    if not settings.search_free_ddg or choice not in ("auto", "ddg"):
        return None
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        log.info("search.ddg.unavailable", reason="curl_cffi 미설치(bypass extra)")
        return None
    return DdgProvider(settings)


def build_search_provider(
    settings: Settings,
    *,
    fetcher: SupportsFetch | None = None,
    cost_ledger: SupportsCostLedger | None = None,
    rate_limiters: HostRateLimiters | None = None,
) -> SearchProvider | None:
    """설정에 맞는 검색 공급자를 만든다. 사용 가능한 백엔드가 없으면 None(no-op).

    ``auto``(기본): serper 키 우선, 없으면 cse(키+cx). ``serper``/``cse``: 강제(키 없으면
    None). ``none``: 비활성. ``rate_limiters`` 는 ``fetcher`` 미주입 시 내부 Fetcher 가
    쓸 공유 호스트별 레이트리미터다(세그먼트 병렬 발견의 429 방지).
    """
    choice = (settings.search_provider or "auto").strip().lower()
    if choice == "none":
        return None
    serper_ok = bool(settings.serper_api_key)
    cse_ok = bool(settings.google_cse_key and settings.google_cse_cx)
    if (choice == "serper" or (choice == "auto" and serper_ok)) and serper_ok:
        return SerperProvider(
            settings, fetcher=fetcher, cost_ledger=cost_ledger, rate_limiters=rate_limiters
        )
    if (choice == "cse" or (choice == "auto" and cse_ok)) and cse_ok:
        return CseProvider(
            settings, fetcher=fetcher, cost_ledger=cost_ledger, rate_limiters=rate_limiters
        )
    return None
