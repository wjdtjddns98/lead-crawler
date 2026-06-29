"""실존성 검증 — 제약 ②(현 시점 실존 기업만).

신호를 **다중화**해 단일 HTTP HEAD 의존을 줄인다:
- 도메인 DNS 생존(A/MX 레코드 존재) — 사이트가 잠깐 죽어도 도메인 생존을 포착,
- 홈페이지 HTTP 200 — 실제 서비스 생존,
- (opt-in seam) 등록처 active 신호 — EDGAR/DART/GLEIF 등 공식 등록처가 active/최근
  공시로 보고하면 가장 강한 신호로 우선한다(주입형, 미주입이면 미사용).

판정은 위 신호를 **등급화 confidence** 로 합성한다(둘 다=높음, 하나=중간, 없음=비실존).
모든 프로브는 주입 가능(테스트는 네트워크 없이 가짜 프로브로 분기 검증). dry_run 에서는
네트워크 없이 도메인 유무로 결정적 판정한다.
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel

from ..config import Settings, get_settings
from ..logging import get_logger

log = get_logger("verify.existence")

# 파킹/판매중 도메인 표지(소문자 비교). 강한 다어절 표현만 — 오탐(정상 사이트가 단어를
# 우연히 포함) 회피. **가시 텍스트** 기준으로만 매치하고(스크립트/속성 오탐 차단), 마커가
# 있어도 본문이 풍부하면(레지스트라/호스팅사 제품설명 등) 파킹으로 보지 않는다.
_PARKING_MARKERS = (
    "domain is for sale",
    "buy this domain",
    "this domain is parked",
    "domain may be for sale",
    "this domain may be for sale",
    "the domain has expired",
    "domain is parked free",
    "이 도메인은 판매",
    "도메인을 구매하",
    "주차된 도메인",
)
# 가시 텍스트가 이 길이 미만이고 **구조 신호(링크·이미지)도 없으면** JS-blank/빈 페이지로 본다.
# 구조 신호를 요구해 이미지-only 소규모 정상 홈페이지(텍스트 적음)를 오탐하지 않는다(제약② 보존).
_MIN_BODY_TEXT = 20
# 파킹 표지가 있어도 가시 텍스트가 이 길이 이상이면 정상(마커를 제품명으로 쓰는 레지스트라 등).
_PARKING_MAX_TEXT = 200
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# script/style **내용**까지 제거(태그만 떼면 minified JS 가 본문에 남아 길이·마커가 오염됨).
_SCRIPT_STYLE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_STRUCTURE = re.compile(r"(?i)<(a[ >]|img)")


def looks_parked(html: str | None) -> bool:
    """렌더/응답 HTML 이 파킹·판매중·JS-blank(실접속 생존 아님)로 보이면 True.

    script/style 내용을 제거한 **가시 텍스트** 기준으로 판정한다(제약② — 정상 기업을
    떨구지 않도록 보수적):
    - 파킹 표지가 가시 텍스트에 있고 **본문이 빈약**(< _PARKING_MAX_TEXT)할 때만 파킹(마커를
      제품명으로 쓰는 레지스트라/호스팅 홈페이지는 본문이 풍부해 제외).
    - 마커가 없어도 가시 텍스트가 거의 없고 **링크·이미지 구조도 없으면** 빈/JS-blank(죽음).
      이미지-only 정상 홈페이지(img 보유)는 구조 신호로 보존.
    """
    if not html or not html.strip():
        return True
    cleaned = _SCRIPT_STYLE.sub(" ", html)
    text = _WS.sub(" ", _TAG.sub(" ", cleaned)).strip().lower()
    if any(marker in text for marker in _PARKING_MARKERS) and len(text) < _PARKING_MAX_TEXT:
        return True
    if len(text) < _MIN_BODY_TEXT and _STRUCTURE.search(cleaned) is None:
        return True
    return False


class ExistenceResult(BaseModel):
    """실존성 판정 결과."""

    is_active: bool
    site_alive: bool
    confidence: float


class SupportsSiteProbe(Protocol):
    """홈페이지 HTTP 생존 프로버(테스트 더블이 구현)."""

    def head_ok(self, domain: str) -> bool:
        """``https?://domain`` HEAD 가 200~399 면 True(실패 시 False)."""
        ...


class SupportsDnsProbe(Protocol):
    """도메인 DNS 생존 프로버(테스트 더블이 구현)."""

    def resolves(self, domain: str) -> bool:
        """도메인이 A 또는 MX 레코드로 해석되면 True(실패 시 False)."""
        ...


class SupportsRender(Protocol):
    """헤드리스 렌더러(테스트 더블·Playwright 가 구현) — JS 실행 후 HTML 반환."""

    def render(self, domain: str) -> str | None:
        """``https?://domain`` 을 헤드리스로 렌더해 최종 HTML 을 반환(실패 시 None)."""
        ...

    def close(self) -> None:
        """브라우저/리소스를 정리한다(재사용 렌더러의 종료 훅; ExistenceVerifier.close 가 호출)."""
        ...


class SupportsRegistryActive(Protocol):
    """등록처 active 신호 체커(주입형 placeholder — 미주입이면 미사용)."""

    def is_active(self, registry: str | None, registry_id: str | None) -> bool | None:
        """등록처가 active/defunct 를 보고하면 True/False, 판정 불가면 None.

        **계약**: 룩업 실패·미지원 등록처·불확실은 반드시 ``None`` 을 반환해야 한다.
        ``False`` 는 등록처가 **명시적으로 defunct** 로 보고한 경우만 — False 는 실존 기업을
        reject(저장 차단)하므로 룩업 오류를 False 로 흘리면 안 된다.
        """
        ...


class HttpSiteProbe:
    """httpx 기반 실 HTTP HEAD 프로버(graceful — 오류 시 False)."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def head_ok(self, domain: str) -> bool:
        import httpx

        for scheme in ("https", "http"):
            url = f"{scheme}://{domain}"
            try:
                resp = httpx.head(url, timeout=self._timeout, follow_redirects=True)
            except Exception as exc:  # 연결 실패·타임아웃 등 → 다음 스킴.
                log.debug("existence.http.fail", domain=domain, scheme=scheme, err=str(exc))
                continue
            # B2: HEAD 차단(405/501 미지원, 403 WAF/안티봇)이면 GET 폴백 — 살아있는데 HEAD 만
            # 막힌 사이트의 오탐(false-negative=리드손실)을 줄인다. GET 도 죽음/파킹이면 그대로 탈락.
            if resp.status_code in (403, 405, 501):
                if self._get_alive(url):
                    return True
                continue
            if resp.status_code < 400:
                return True
        return False

    def _get_alive(self, url: str) -> bool:
        """GET 으로 생존을 재확인한다(B2) — 200대이고 본문이 파킹/blank 가 아니면 True."""
        import httpx

        try:
            resp = httpx.get(url, timeout=self._timeout, follow_redirects=True)
        except Exception as exc:
            log.debug("existence.http.get_fail", url=url, err=str(exc))
            return False
        if resp.status_code >= 400:
            return False
        if looks_parked(resp.text):  # GET 본문이 파킹/판매중/blank → 실접속 생존 아님.
            log.info("existence.http.parked", url=url)
            return False
        return True


class DnsProbe:
    """dnspython 기반 실 DNS 프로버 — A 또는 MX 존재 여부(graceful)."""

    def resolves(self, domain: str) -> bool:
        import dns.resolver

        # dnspython 은 레코드 없으면 NoAnswer/NXDOMAIN 을 raise → 성공 호출 자체가 존재 증거.
        for rtype in ("A", "MX"):
            try:
                dns.resolver.resolve(domain, rtype)
                return True
            except Exception as exc:  # NoAnswer·NXDOMAIN·타임아웃 → 다음 레코드.
                log.debug("existence.dns.fail", domain=domain, rtype=rtype, err=str(exc))
                continue
        return False


class ExistenceVerifier:
    """도메인/등록처 신호로 기업 실존 여부를 판정한다."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        site_probe: SupportsSiteProbe | None = None,
        dns_probe: SupportsDnsProbe | None = None,
        registry_checker: SupportsRegistryActive | None = None,
        render_probe: SupportsRender | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._site_probe = site_probe
        self._dns_probe = dns_probe
        self._registry_checker = registry_checker
        self._render_probe = render_probe

    def close(self) -> None:
        """지연 생성한 프로브 자원을 정리한다(best-effort). registry_checker 는 호출부가 정리."""
        for probe in (self._site_probe, self._dns_probe, self._render_probe):
            close = getattr(probe, "close", None)
            if callable(close):
                close()

    def verify(
        self,
        domain: str | None,
        *,
        registry: str | None = None,
        registry_id: str | None = None,
        home_html: str | None = None,
    ) -> ExistenceResult:
        """도메인 생존(DNS+HTTP) + 등록처 신호로 실존성을 등급화 산정한다.

        ``home_html`` 이 주어지면(enrich 가 같은 도메인 home 을 이미 200 으로 GET 한 경우)
        그 본문으로 생존을 판정해 별도 HEAD/GET 프로브를 생략한다(기업당 중복 왕복 제거).
        본문이 파킹/JS-blank 면 생존 아님 — head_ok 의 GET폴백 looks_parked 검사와 동치(추가
        왕복 0). None 이면(enrich dry_run·도메인없음·fetch실패) 기존대로 head_ok 로 프로브.
        verify_headless(opt-in) 가 켜지면 정적 파킹/blank 의심분은 단정 않고 렌더 검사로 최종
        판정한다(bare SPA recall 보호); 꺼져 있으면 정적 본문 판정이 최종이다.
        """
        if self.settings.dry_run:
            alive = bool(domain)
            return ExistenceResult(
                is_active=alive, site_alive=alive, confidence=0.9 if alive else 0.0
            )

        if not domain:
            site_alive = False
        elif home_html is not None:
            # enrich 의 성공 GET 본문을 재사용 — head_ok 의 GET폴백 looks_parked 검사와 동치하게
            # 파킹/JS-blank 본문은 비생존으로 본다(제약② 강화, 추가 왕복 0). 단 verify_headless
            # 가 켜졌는데 정적 본문이 파킹/blank 로 보이면 정적 GET 으로는 모호한 bare SPA 일 수
            # 있으므로 단정하지 않고 아래 헤드리스 렌더 검사에 위임한다(정상 SPA recall 보호).
            static_alive = not looks_parked(home_html)
            site_alive = (
                True if (not static_alive and self.settings.verify_headless) else static_alive
            )
        else:
            site_alive = self._site().head_ok(domain)
        # B1 헤드리스 확인(opt-in) — HTTP 가 살아있다 해도 파킹/JS-blank 면 실접속 생존 아님.
        # site_alive 후보만 렌더(불필요한 렌더 회피). 렌더 실패(None)는 graceful 통과(기존 판정
        # 유지) — 헤드리스 미설치로 실존 기업을 떨구지 않기 위함. 파킹/blank 확인 시에만 떨군다.
        if site_alive and domain and self.settings.verify_headless:
            rendered = self._render().render(domain)
            if rendered is not None and looks_parked(rendered):
                log.info("existence.headless.parked", domain=domain)
                site_alive = False
        dns_alive = self._dns().resolves(domain) if domain else False

        # 등록처 active 신호(주입 시) — 가장 강한 신호로 우선한다.
        registry_active = (
            self._registry_checker.is_active(registry, registry_id)
            if self._registry_checker is not None
            else None
        )
        if registry_active is True:
            result = ExistenceResult(is_active=True, site_alive=site_alive, confidence=0.9)
        elif registry_active is False:
            # 등록처가 defunct 로 보고 — 사이트가 살아있어도 실존 아님(제약 ②). 높은 신뢰.
            result = ExistenceResult(is_active=False, site_alive=site_alive, confidence=0.9)
        else:
            # 등록처 신호 없음 → **HTTP 서비스 생존을 admit 기준**으로 한다(제약 ②: 현 시점
            # 실존). DNS 는 단독 admit 신호가 아니라(parked domain 도 해석됨) 살아있는 사이트를
            # 보강하는 confidence 신호로만 쓴다 — DNS-only 는 비실존으로 보수 처리.
            if site_alive and dns_alive:
                confidence = 0.85  # HTTP+DNS 일치 — 강한 실존.
            elif site_alive:
                confidence = 0.7  # HTTP 만 — 서비스 생존(DNS 조회 실패/누락).
            else:
                confidence = 0.0  # 사이트 미생존(DNS 만 있어도 admit 안 함).
            result = ExistenceResult(
                is_active=site_alive, site_alive=site_alive, confidence=confidence
            )
        log.info(
            "existence.verify",
            domain=domain or "",
            site=site_alive,
            dns=dns_alive,
            registry=registry_active,
            active=result.is_active,
            confidence=result.confidence,
        )
        return result

    def _site(self) -> SupportsSiteProbe:
        if self._site_probe is None:
            self._site_probe = HttpSiteProbe(timeout=self.settings.http_timeout)
        return self._site_probe

    def _dns(self) -> SupportsDnsProbe:
        if self._dns_probe is None:
            self._dns_probe = DnsProbe()
        return self._dns_probe

    def _render(self) -> SupportsRender:
        if self._render_probe is None:
            self._render_probe = PlaywrightRender(timeout=self.settings.headless_timeout)
        return self._render_probe


class PlaywrightRender:
    """Playwright 헤드리스 렌더러 — lazy 기동·재사용·graceful 실패(미설치/오류 시 None).

    기존엔 render() 호출마다 Chromium 을 새로 띄우고 닫아 기업당 콜드스타트(수백 ms~초)를
    물었다. enrich/headless.PlaywrightRenderer 와 동일하게 브라우저를 1회 기동해 재사용한다
    (페이지만 매 호출 생성·정리). ExistenceVerifier.close() 가 종료 시 close() 를 호출해
    자원을 정리한다. 인스턴스는 워커 스레드 전용(run.py)이라 브라우저 재사용은 스레드안전하다.
    """

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._timeout_ms = int(timeout * 1000)
        self._pw = None
        self._browser = None
        self._unavailable = False  # 미설치/기동실패 시 재시도 안 함.

    def _ensure(self) -> bool:
        """브라우저를 1회 기동(재사용). 미설치/실패면 False(이후 비활성)."""
        if self._browser is not None:
            return True
        if self._unavailable:
            return False
        try:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            return True
        except Exception as exc:  # 미설치(ImportError)·브라우저 미설치·기동실패 → graceful.
            log.info("existence.render.unavailable", err=str(exc))
            self._unavailable = True
            return False

    def render(self, domain: str) -> str | None:
        """domain 을 https→http 순으로 렌더해 최종 HTML 을 반환(전부 실패/미설치 시 None)."""
        if not self._ensure():
            return None
        page = None
        try:
            page = self._browser.new_page()
            for scheme in ("https", "http"):
                try:
                    page.goto(
                        f"{scheme}://{domain}",
                        timeout=self._timeout_ms,
                        wait_until="domcontentloaded",
                    )
                    return page.content()
                except Exception as exc:  # 해당 스킴 렌더 실패 → 다음 스킴.
                    log.debug("existence.render.fail", domain=domain, scheme=scheme, err=str(exc))
                    continue
            return None
        except Exception as exc:  # new_page 등 예기치 못한 실패 → graceful None.
            log.info("existence.render.error", domain=domain, err=str(exc))
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # 정리 실패는 무시(베스트에포트).
                    pass

    def close(self) -> None:
        """브라우저·Playwright 를 정리한다(커넥션 누수 방지)."""
        for obj, method in ((self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:  # 정리 실패는 무시(베스트에포트).
                    pass
        self._browser = None
        self._pw = None
