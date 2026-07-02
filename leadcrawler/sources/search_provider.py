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

from typing import Protocol

from ..config import Settings
from ..cost_ledger import SupportsCostLedger
from ..logging import get_logger
from .http import Fetcher, HostRateLimiters, SupportsFetch

log = get_logger("sources.search_provider")

_CSE_URL = "https://customsearch.googleapis.com/customsearch/v1"
_SERPER_URL = "https://google.serper.dev/search"
_NAVER_URL = "https://openapi.naver.com/v1/search/webkr.json"


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

    def fetch_page(self, query: str, *, gl: str, lr: str, start: int) -> list[dict]:
        if self._budget_blocked():
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
            log.info("search.serper.error", page=page, err=str(exc))
            return []
        self._record()  # 호출 1건 = 1 크레딧.
        organic = payload.get("organic") if isinstance(payload, dict) else None
        return [it for it in (organic or []) if isinstance(it, dict)]

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
    """네이버 검색 API(웹문서 webkr). 무료 25,000쿼리/일 — KR 기업 도메인 해석용.

    KR 한정 백엔드라 :func:`build_search_provider` 선택 사다리(글로벌 SERP)에는 넣지 않고,
    :func:`build_naver_provider` 로 별도 구성해 호출부(DomainResolver)가 KR 기업에만
    라우팅한다. ``gl``/``lr`` 은 무의미(네이버=한국 웹)라 무시한다. 무과금(원장 기록 없음).
    """

    name = "naver"
    page_size = 30  # webkr display 상한.
    max_start = 100  # webkr start 상한.

    def fetch_page(self, query: str, *, gl: str, lr: str, start: int) -> list[dict]:
        params = {"query": query, "display": self.page_size, "start": start}
        headers = {
            "X-Naver-Client-Id": self._settings.naver_client_id,
            "X-Naver-Client-Secret": self._settings.naver_client_secret,
        }
        try:
            payload = self._fetcher().get_json(_NAVER_URL, params=params, headers=headers)
        except Exception as exc:  # 검색 실패는 빈 페이지(호출부가 miss 처리).
            log.info("search.naver.error", start=start, err=str(exc))
            return []
        items = payload.get("items") if isinstance(payload, dict) else None
        return [it for it in (items or []) if isinstance(it, dict)]


def build_naver_provider(
    settings: Settings,
    *,
    fetcher: SupportsFetch | None = None,
    rate_limiters: HostRateLimiters | None = None,
) -> SearchProvider | None:
    """네이버 공급자를 만든다(Client ID/Secret 둘 다 필요, 없으면 None)."""
    if not (settings.naver_client_id and settings.naver_client_secret):
        return None
    return NaverProvider(settings, fetcher=fetcher, rate_limiters=rate_limiters)


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
