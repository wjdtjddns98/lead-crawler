"""일본 금융 협회 회원명부 발견 소스 — 증권업협회(JSDA)·資産運用業協会(IMAJ) 회원사 홈페이지 목록.

두 협회가 공개하는 회원사 홈페이지 목록 HTML 을 읽어 **상호+공식 도메인**을 열거한다
(2026-08-31 실측: JSDA 회원 206·특정업무 23·특별회원(등록금융기관=은행·신금) 74, IMAJ
운용회원 350·조언대리회원 253 — 고유 도메인 801, 그중 원장 미보유 681). 무인증·무과금·
고정 URL·정적 HTML 이라 stdlib(html.parser) 만으로 파싱한다(새 의존성 0).

- 산출 필드: 일문 상호·도메인(``dom:`` 키). 등록번호는 페이지에 없어 registry 키 없음 —
  같은 회사가 EDINET(reg:edinet, 도메인 해석 후) 과 겹치면 파이프라인의 도메인 동치
  dedup(제약 ①) 이 첫 등장(등록처 tier) 우선으로 병합한다.
- 업종: 페이지 단위 고정 라벨(JSDA 회원·특정업무·IMAJ 전부 → 증권·자산운용, JSDA
  특별회원 → 은행). 투자조언·대리 회원도 '증권·자산운용'(투자자문 라벨 미사용 규칙).
- 상장여부: 페이지가 알려주지 않음 → 세그먼트 스코프값 그대로(listed_verified=False).
  ``listed='listed'`` 세그먼트엔 **적용하지 않는다** — 상장 JP 는 EDINET 이 전수 커버하고,
  비상장 다수를 상장 스코프값으로 흘리면 원장 오염(스코프값 사실 고착 실사고 계열).
- 커서: 페이지당 1 GET 으로 전 모집단이 오므로 EDINET 과 같은 **로컬 슬라이스**
  (``cursor_offset/advance_cursor``, 키=segment.label). 네트워크는 인스턴스당 페이지별 1콜(메모).
- 표시명: 페이지에 영문 상호가 없어 일문 그대로(EDINET 영문 우선 규칙의 예외 — 음역
  후속 트랙과 동일 처리). 하류 resolve 가 도메인을 이미 가진 행은 건드리지 않는다.
- dry_run 은 네트워크 없이 결정적 더미(전 소스 계약).

ponytail: 회원 전화·업무구분(IMAJ 표 열)·홈페이지 없는 회원(name: 키)은 안 담는다 —
전화는 enrich 가 홈페이지에서 뽑고, 도메인 없는 회원은 이 소스의 가치(도메인 동봉)
밖이다. 금융청 登録一覧(1,951사·법인번호·URL 없음)은 gBizINFO 토큰 확보 시 2단계로.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urlparse

from ..config import Settings
from ..logging import get_logger
from .base import (
    DiscoveredCompany,
    Segment,
    SupportsCursorStore,
    advance_cursor,
    build_company,
    cursor_offset,
)
from .countries import resolve_country
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import is_broad_industry, resolve_industry_label

log = get_logger("sources.jp_assoc")

_SEC = "증권·자산운용"
_BANK = "은행"

# (URL, 업종 라벨) — 페이지 단위 고정 라벨. 순서 = 신뢰도(증권사 본체 → 운용사 → 조언).
_PAGES: tuple[tuple[str, str], ...] = (
    ("https://www.jsda.or.jp/kyoukaiin/kyoukaiin/website/kaiin.html", _SEC),
    ("https://www.jsda.or.jp/kyoukaiin/kyoukaiin/website/tokuteigyoumu.html", _SEC),
    ("https://www.jsda.or.jp/kyoukaiin/kyoukaiin/website/tokubetu.html", _BANK),
    ("https://www.imaj.or.jp/members/list/investment_management/", _SEC),
    ("https://www.imaj.or.jp/members/list/advice/", _SEC),
)
_LABELS = frozenset(label for _, label in _PAGES)

# 페이지 내 외부 링크 중 회원사가 아닌 것(협회 자체·구 협회·SNS·통계 링크) — 호스트 접미 일치.
_SKIP_HOSTS = (
    "jsda.or.jp", "imaj.or.jp", "toushin.or.jp", "jiaa.or.jp",
    "x.com", "twitter.com", "facebook.com", "youtube.com", "instagram.com",
)
_LINK_SUFFIX = "(別ウィンドウで開く)"


class _MemberLinks(HTMLParser):
    """``<main>`` 안의 ``<a href=외부URL>상호</a>`` 를 (상호, href) 로 수집한다.

    앵커 안의 아이콘 텍스트("(別ウィンドウで開く)")는 제거. ``<main>`` 이 없는 문서(포맷
    변경)는 전체를 대상으로 한다 — 헤더 SNS 링크는 호스트 블록리스트가 거른다.
    """

    def __init__(self) -> None:
        super().__init__()
        self._all: list[tuple[str, str, bool]] = []  # (상호, href, main 안 여부)
        self._in_main = False
        self._saw_main = False
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "main":
            self._in_main = True
            self._saw_main = True
            return
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if href.startswith(("http://", "https://")):
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "main":
            self._in_main = False
            return
        if tag != "a" or self._href is None:
            return
        name = " ".join("".join(self._text).split()).replace(_LINK_SUFFIX, "").strip()
        if name:
            self._all.append((name, self._href, self._in_main))
        self._href = None
        self._text = []

    @property
    def links(self) -> list[tuple[str, str]]:
        """``<main>`` 이 있으면 그 안의 앵커만(헤더 SNS·푸터 배너는 main 앞뒤에 옴)."""
        return [(n, h) for n, h, inside in self._all if inside or not self._saw_main]


def _domain_of(url: str) -> str | None:
    """href → 호스트(소문자, www. 제거). 블록리스트 호스트·비정상 URL 은 None."""
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    if any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS):
        return None
    return host


def parse_members(html: str) -> list[tuple[str, str]]:
    """회원명부 HTML → [(상호, 도메인)] (문서 내 순서, 도메인 중복 제거).

    ``<main>`` 밖 앵커는 버리고(헤더/푸터), 블록리스트 호스트는 제외한다. 파싱 예외는
    빈 목록(graceful — 상류 포맷 변경은 호출부가 0건 경고로 가시화).
    """
    parser = _MemberLinks()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # html.parser 는 관대하지만 방어(포맷 붕괴 → 0건).
        log.info("jp_assoc.parse_error", err=str(exc))
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, href in parser.links:
        dom = _domain_of(href)
        if dom is None or dom in seen:
            continue
        seen.add(dom)
        out.append((name, dom))
    return out


class JpAssocSource:
    """JSDA·IMAJ 회원명부 기반 일본 금융사 발견 소스(협회 명부 tier — 등록처 뒤·집계원 앞)."""

    name = "jp_assoc"

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
        # 페이지별 fetch-once 메모(실패는 메모하지 않음 — EDINET 과 같은 침묵 고착 방지).
        self._page_memo: dict[str, list[tuple[str, str]]] = {}

    def applies_to(self, segment: Segment) -> bool:
        """JP 전용 + 비상장/미상 스코프 + (broad 또는 은행·증권·자산운용)."""
        country = resolve_country(segment.country)
        if country is None or country.iso2 != "JP":
            return False
        if segment.listed == "listed":
            return False  # 상장은 EDINET 전수 — 스코프값 오염 방지(모듈 docstring).
        if is_broad_industry(segment.industry):
            return True
        return resolve_industry_label(segment.industry) in _LABELS

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        if self._settings.dry_run:
            return self._dry(segment)
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(dom: 키 — 라이브와 같은 키 티어)."""
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} JP Assoc Co {i}",
                domain=f"jp-assoc{i}.co.jp",
                industry_code_label=_SEC,
            )
            for i in range(self._count)
        ]

    def _client(self) -> SupportsFetch:
        if self._fetcher is None:
            self._fetcher = Fetcher(
                user_agent=self._settings.discovery_user_agent,
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
                rate_limiters=self._rate_limiters,
            )
        return self._fetcher

    def _members(self, url: str) -> list[tuple[str, str]]:
        """페이지 1개의 (상호, 도메인) — 런 내 1회 fetch·메모. 실패/0건은 메모하지 않는다."""
        cached = self._page_memo.get(url)
        if cached is not None:
            return cached
        try:
            html = self._client().get_text(url)
        except Exception as exc:  # 네트워크/차단 → 이번 세그먼트만 빈 결과(재시도 가능).
            log.info("jp_assoc.fetch_error", url=url, err=str(exc))
            return []
        members = parse_members(html)
        if not members:
            # 200 + 포맷 변경/오류 페이지 — 고착시키지 않고 경고(침묵 0건 방지).
            log.warning("jp_assoc.parse_empty", url=url)
            return []
        self._page_memo[url] = members
        return members

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """(구체 업종이면 라벨 일치 페이지만) → 페이지 순 병합 → 커서 슬라이스 → 캡."""
        cap = self._settings.discovery_max_per_source
        if cap <= 0:
            return []
        want: str | None = None
        if not is_broad_industry(segment.industry):
            want = resolve_industry_label(segment.industry)
        pool: list[tuple[str, str, str]] = []  # (상호, 도메인, 라벨)
        seen: set[str] = set()
        for url, label in _PAGES:
            if want is not None and label != want:
                continue
            for name, dom in self._members(url):
                if dom in seen:
                    continue  # 협회 겸업(증권사가 IMAJ 에도 등재) — 첫 페이지 라벨 우선.
                seen.add(dom)
                pool.append((name, dom, label))

        start = cursor_offset(self._cursor_store, self.name, segment, len(pool))
        out: list[DiscoveredCompany] = []
        pos = start
        for name, dom, label in pool[start:]:
            pos += 1
            out.append(
                build_company(
                    source=self.name,
                    segment=segment,
                    name=name,
                    domain=dom,
                    industry_code_label=label,
                )
            )
            if len(out) >= cap:
                break
        advance_cursor(self._cursor_store, self.name, segment, pos, len(pool))
        log.info("jp_assoc.live", segment=segment.label, pool=len(pool), n=len(out), start=start)
        return out
