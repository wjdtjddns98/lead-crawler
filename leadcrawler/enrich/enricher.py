"""연락처 보강 오케스트레이터 — 발견 기업 도메인에서 IR이메일·전화·문의폼 추출.

- dry_run: 네트워크 없이 도메인 기반 결정적 더미.
- live(정적): 홈페이지 + IR/문의 후보 페이지를 BFS(상한 ``enrich_max_pages``)로 받아
  :mod:`leadcrawler.enrich.extract` 로 파싱. 이메일은 HR/언론 배제, 페이지 간 dedup.
헤드리스/OCR/비전 escalation 은 후속(정적으로 충분치 않은 기업 대상).
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ..logging import get_logger
from ..models import Contact, ContactType, EmailRole, ExtractMethod
from ..sources.base import DiscoveredCompany
from ..sources.http import Fetcher, SupportsFetch
from .extract import candidate_links, extract_emails, extract_form, extract_phones
from .headless import PlaywrightRenderer, SupportsRender

log = get_logger("enrich")


class Enricher:
    """기업 1건의 연락처를 추출한다(dry_run 더미 / 정적 라이브 / 헤드리스 escalation)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        fetcher: SupportsFetch | None = None,
        renderer: SupportsRender | None = None,
    ):
        self._settings = settings or get_settings()
        self._fetcher = fetcher
        self._renderer = renderer

    def enrich(self, dc: DiscoveredCompany) -> list[Contact]:
        """발견 기업의 연락처 후보(이메일·전화·폼) 목록을 반환한다."""
        if self._settings.dry_run:
            return _dry_contacts(dc)
        if not dc.domain:
            return []
        contacts = self._live(dc)
        # 정적으로 이메일을 못 찾았고 헤드리스가 켜져 있으면 JS 렌더로 escalate.
        if self._settings.enrich_headless and not _has_email(contacts):
            contacts = self._escalate(dc, contacts)
        return contacts

    def _client(self) -> SupportsFetch:
        if self._fetcher is None:
            self._fetcher = Fetcher(
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
            )
        return self._fetcher

    def _renderer_obj(self) -> SupportsRender:
        if self._renderer is None:
            self._renderer = PlaywrightRenderer(timeout=self._settings.headless_timeout)
        return self._renderer

    def close(self) -> None:
        """내부에서 만든 httpx 클라이언트·렌더러를 정리한다(리소스 누수 방지)."""
        for obj in (self._fetcher, self._renderer):
            close = getattr(obj, "close", None)
            if callable(close):
                close()

    def _escalate(self, dc: DiscoveredCompany, static_contacts: list[Contact]) -> list[Contact]:
        """헤드리스로 홈·IR/문의 페이지를 렌더해 JS 이메일/폼을 보강한다.

        렌더 실패(미설치 포함)면 정적 결과를 그대로 유지한다. 헤드리스로 찾은
        이메일은 ``ExtractMethod.HEADLESS`` 로 출처를 표기한다. 정적 전화는 보존.
        """
        renderer = self._renderer_obj()
        cap = max(1, self._settings.enrich_max_pages)
        home = f"https://{dc.domain}"
        home_html = renderer.render(home)
        if home_html is None:  # 렌더 불가(미설치·실패) → 정적 결과 유지.
            return static_contacts

        links = candidate_links(home_html, base_url=home, domain=dc.domain, limit=cap)
        pages = [home, *links][:cap]
        emails: dict[str, Contact] = {}
        form: Contact | None = next(
            (c for c in static_contacts if c.type is ContactType.FORM), None
        )
        for url in pages:
            html = home_html if url == home else renderer.render(url)
            if html is None:
                continue
            for c in extract_emails(html, source_url=url, method=ExtractMethod.HEADLESS):
                cur = emails.get(c.value)
                if cur is None or c.confidence > cur.confidence:
                    emails[c.value] = c
            if form is None:
                form = extract_form(html, page_url=url, method=ExtractMethod.HEADLESS)

        phones = [c for c in static_contacts if c.type is ContactType.PHONE]
        out: list[Contact] = [*emails.values(), *phones]
        if form is not None:
            out.append(form)
        log.info("enrich.headless", domain=dc.domain, emails=len(emails), form=form is not None)
        return out

    def _live(self, dc: DiscoveredCompany) -> list[Contact]:
        """정적 BFS 로 홈페이지·IR/문의 페이지를 훑어 연락처를 모은다."""
        fetcher = self._client()
        cap = max(1, self._settings.enrich_max_pages)
        home = f"https://{dc.domain}"
        try:
            home_html = fetcher.get_text(home)
        except Exception as exc:
            log.info("enrich.home.error", domain=dc.domain, err=str(exc))
            return []

        links = candidate_links(home_html, base_url=home, domain=dc.domain, limit=cap)
        pages = [home, *links][:cap]
        emails: dict[str, Contact] = {}
        phones: dict[str, Contact] = {}
        form: Contact | None = None

        for url in pages:
            html = home_html if url == home else _safe_get(fetcher, url)
            if html is None:
                continue
            for c in extract_emails(html, source_url=url):
                cur = emails.get(c.value)
                if cur is None or c.confidence > cur.confidence:
                    emails[c.value] = c
            for c in extract_phones(html, source_url=url):
                phones.setdefault(c.value, c)
            if form is None:
                form = extract_form(html, page_url=url)

        out: list[Contact] = [*emails.values(), *phones.values()]
        if form is not None:
            out.append(form)
        log.info(
            "enrich.live", domain=dc.domain, emails=len(emails),
            phones=len(phones), form=form is not None,
        )
        return out


def _has_email(contacts: list[Contact]) -> bool:
    """채택된 이메일 연락처가 하나라도 있는지."""
    return any(c.type is ContactType.EMAIL for c in contacts)


def _safe_get(fetcher: SupportsFetch, url: str) -> str | None:
    try:
        return fetcher.get_text(url)
    except Exception as exc:  # 개별 페이지 실패는 건너뛴다.
        log.info("enrich.page.error", url=url, err=str(exc))
        return None


def _dry_contacts(dc: DiscoveredCompany) -> list[Contact]:
    """dry_run 보강 — 도메인 기반 결정적 연락처(이메일·전화·폼)."""
    domain = dc.domain or "example.com"
    return [
        Contact(
            type=ContactType.EMAIL, value=f"ir@{domain}", role=EmailRole.IR,
            extract_method=ExtractMethod.STATIC,
            source_url=f"https://{domain}/investor", confidence=0.9,
        ),
        Contact(
            type=ContactType.PHONE, value="+82-2-0000-0000",
            extract_method=ExtractMethod.STATIC, confidence=0.6,
        ),
        Contact(
            type=ContactType.FORM, value=f"https://{domain}/contact",
            extract_method=ExtractMethod.STATIC, confidence=0.8,
        ),
    ]
