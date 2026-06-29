"""연락처 보강 오케스트레이터 — 발견 기업 도메인에서 IR이메일·전화·문의폼 추출.

- dry_run: 네트워크 없이 도메인 기반 결정적 더미.
- live(정적): 홈페이지 + IR/문의 후보 페이지를 BFS(상한 ``enrich_max_pages``)로 받아
  :mod:`leadcrawler.enrich.extract` 로 파싱. 이메일은 HR/언론 배제, 페이지 간 dedup.
헤드리스/OCR/비전 escalation 은 후속(정적으로 충분치 않은 기업 대상).
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from ..config import Settings, get_settings
from ..cost_ledger import SupportsCostLedger
from ..logging import get_logger
from ..models import Contact, ContactType, EmailRole, ExtractMethod
from ..sources.base import DiscoveredCompany
from ..sources.http import Fetcher, SupportsFetch
from .extract import (
    candidate_images,
    candidate_links,
    emails_from_text,
    extract_emails,
    extract_form,
    extract_phones,
    is_contact_page,
)
from .emailapi import ApolloFinder, HunterFinder, SupportsEmailFinder
from .headless import PlaywrightRenderer, SupportsRender
from .ocr import SupportsOcr, TesseractOcr
from .vision import ClaudeVision, SupportsVision, media_type_for

log = get_logger("enrich")


class Enricher:
    """기업 1건의 연락처를 추출한다(정적 → 헤드리스 → OCR → EmailAPI → Vision, dry_run 더미)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        fetcher: SupportsFetch | None = None,
        renderer: SupportsRender | None = None,
        ocr: SupportsOcr | None = None,
        vision: SupportsVision | None = None,
        email_finders: list[SupportsEmailFinder] | None = None,
        cost_ledger: SupportsCostLedger | None = None,
    ):
        self._settings = settings or get_settings()
        self._fetcher = fetcher
        self._renderer = renderer
        self._ocr = ocr
        self._vision = vision
        self._email_finders = email_finders
        self._cost_ledger = cost_ledger
        self._contact_page: str | None = None  # 현재 기업의 문의페이지 힌트(폴백용).
        self._home_html_cache: str | None = None  # 현재 기업의 home HTML(에스컬레이션 단계 간 재사용).

    def enrich(self, dc: DiscoveredCompany) -> list[Contact]:
        """발견 기업의 연락처 후보(이메일·전화·폼) 목록을 반환한다."""
        # 기업별 상태는 진입 즉시 초기화 — 조기 반환(dry_run·도메인없음) 경로에서도 직전
        # 기업 값이 새지 않게 한다(last_home_html 은 실존검증 재사용 신호로 외부 노출됨).
        self._home_html_cache = None
        self._contact_page = None
        if self._settings.dry_run:
            return _dry_contacts(dc)
        if not dc.domain:
            return []
        contacts = self._live(dc)
        # escalation 체인 — 이메일을 못 찾았고 해당 단계가 켜져 있을 때만 순차 시도.
        if self._settings.enrich_headless and not _has_email(contacts):
            contacts = self._escalate(dc, contacts)
        if self._settings.enrich_ocr and not _has_email(contacts):
            contacts = self._escalate_ocr(dc, contacts)
        # 이메일 탐색 API(유료·제3자 DB) — 사이트에서 못 찾았을 때 도메인으로 질의.
        if (
            self._settings.enrich_email_api
            and not _has_email(contacts)
            and not self._budget_blocked()
        ):
            contacts = self._escalate_email_api(dc, contacts)
        # Vision 은 유료 — 키가 있고 플래그가 켜졌을 때만 최후로 시도.
        if (
            self._settings.enrich_vision
            and self._settings.anthropic_api_key
            and not _has_email(contacts)
            and not self._budget_blocked()
        ):
            contacts = self._escalate_vision(dc, contacts)
        # 최후 폴백(제약② 리드손실 방지): 모든 단계에서 이메일·폼 0건이지만 문의페이지가
        # 있으면 그 URL 을 저신뢰 폼으로 채택(JS 렌더 문의폼 구제). 헤드리스가 진짜 폼을
        # 올리거나 비울 기회를 다 준 뒤라, 오탐이면 앞 단계가 정정했을 것이다.
        if self._contact_page and not _has_email(contacts) and not _has_form(contacts):
            contacts = [
                *contacts,
                Contact(
                    type=ContactType.FORM,
                    value=self._contact_page,
                    extract_method=ExtractMethod.STATIC,
                    source_url=self._contact_page,
                    confidence=0.3,
                ),
            ]
        return contacts

    def _budget_blocked(self) -> bool:
        """예산 가드 — 원장이 있고 enforce 가 켜졌고 월 누계가 예산 이상이면 차단."""
        led = self._cost_ledger
        if led is None or not self._settings.cost_budget_enforce:
            return False
        if led.is_over_budget():
            log.info("cost.budget.blocked", budget_krw=self._settings.monthly_budget_krw)
            return True
        return False

    def _record_cost(self, provider: str, units: int = 1) -> None:
        """유료 호출 1건을 원장에 적재(원장 없으면 no-op)."""
        if self._cost_ledger is not None:
            self._cost_ledger.record(provider, units)

    def _client(self) -> SupportsFetch:
        if self._fetcher is None:
            self._fetcher = Fetcher(
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
            )
        return self._fetcher

    @property
    def last_home_html(self) -> str | None:
        """직전 enrich() 의 home HTML(성공 GET 시) 또는 None(dry_run·도메인없음·fetch실패).

        ExistenceVerifier 가 같은 도메인에 별도 HTTP HEAD/GET 을 다시 쏘지 않도록, enrich
        가 이미 받은 home 생존신호를 재사용하는 read-only seam. enrich() 진입마다 초기화된다.
        """
        return self._home_html_cache

    def _home_html(self, fetcher: SupportsFetch, home: str) -> str:
        """기업 1건의 home HTML 을 1회만 받아 재사용한다(정적·OCR·Vision 단계 공유).

        같은 기업의 ``https://{domain}`` 을 단계마다 다시 GET 하던 중복 왕복을 제거한다
        (throttle·TCP/TLS 비용 절감). 캐시는 ``enrich()`` 진입마다 초기화되어 기업 간
        누수가 없고, 인스턴스가 워커 스레드 전용(run.py 워커별 독립 Enricher)이라 스레드안전
        하다. fetch 가 실패하면 캐시에 담지 않아(예외 전파) 단계별 graceful 처리는 보존된다.
        """
        if self._home_html_cache is None:
            self._home_html_cache = fetcher.get_text(home)
        return self._home_html_cache

    def _renderer_obj(self) -> SupportsRender:
        if self._renderer is None:
            self._renderer = PlaywrightRenderer(timeout=self._settings.headless_timeout)
        return self._renderer

    def _ocr_obj(self) -> SupportsOcr:
        if self._ocr is None:
            self._ocr = TesseractOcr()
        return self._ocr

    def _vision_obj(self) -> SupportsVision:
        if self._vision is None:
            self._vision = ClaudeVision(
                self._settings.anthropic_api_key, model=self._settings.vision_model
            )
        return self._vision

    def _email_finders_list(self) -> list[SupportsEmailFinder]:
        """키가 설정된 이메일 탐색 제공자만 우선순위(Hunter→Apollo) 순으로 구성한다."""
        if self._email_finders is None:
            finders: list[SupportsEmailFinder] = []
            if self._settings.hunter_api_key:
                finders.append(
                    HunterFinder(self._settings.hunter_api_key, fetcher=self._client())
                )
            if self._settings.apollo_api_key:
                finders.append(
                    ApolloFinder(self._settings.apollo_api_key, fetcher=self._client())
                )
            self._email_finders = finders
        return self._email_finders

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
        # 헤드리스는 느리고 비싸므로 정적(enrich_max_pages)과 분리된 작은 상한을 쓴다(§4).
        cap = max(1, self._settings.headless_max_pages)
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
            tree = HTMLParser(html or "")  # 페이지당 1회 파싱 — 이메일·폼 추출이 공유.
            for c in extract_emails(html, source_url=url, method=ExtractMethod.HEADLESS, tree=tree):
                cur = emails.get(c.value)
                if cur is None or c.confidence > cur.confidence:
                    emails[c.value] = c
            if form is None:
                form = extract_form(html, page_url=url, method=ExtractMethod.HEADLESS, tree=tree)
            if emails:  # 이메일 확보 시 조기 종료(불필요한 렌더 비용 절감).
                break

        # 산출 = 헤드리스 이메일 + 정적 전화(이미 dedup) + 폼. escalation 은 정적 이메일이
        # 0건일 때만 진입하므로 버려지는 정적 이메일은 없다(연락처 종류는 EMAIL/PHONE/FORM).
        phones = [c for c in static_contacts if c.type is ContactType.PHONE]
        out: list[Contact] = [*emails.values(), *phones]
        if form is not None:
            out.append(form)
        log.info("enrich.headless", domain=dc.domain, emails=len(emails), form=form is not None)
        return out

    def _escalate_ocr(self, dc: DiscoveredCompany, contacts: list[Contact]) -> list[Contact]:
        """홈페이지 이미지(이메일 가능성 높은 것 우선)를 OCR 해 이메일을 보강한다.

        정적·헤드리스로도 이메일이 0건일 때만 진입. OCR 미설치/실패면 ``contacts`` 유지.
        OCR 이메일은 ``ExtractMethod.OCR_VISION`` 으로 표기한다.
        """
        fetcher = self._client()
        ocr = self._ocr_obj()
        home = f"https://{dc.domain}"
        try:
            home_html = self._home_html(fetcher, home)
        except Exception as exc:  # 홈 fetch 실패 → 기존 결과 유지.
            log.info("enrich.ocr.home_error", domain=dc.domain, err=str(exc))
            return contacts

        img_urls = candidate_images(
            home_html, base_url=home, domain=dc.domain, limit=self._settings.ocr_max_images
        )
        emails: dict[str, Contact] = {}
        for url in img_urls:
            data = _safe_bytes(fetcher, url)
            if data is None:
                continue
            for c in emails_from_text(ocr.image_to_text(data), source_url=url):
                emails.setdefault(c.value, c)
            if emails:  # 이메일 확보 시 조기 종료(OCR 비용 절감).
                break
        if not emails:
            return contacts
        log.info("enrich.ocr", domain=dc.domain, emails=len(emails))
        return [*contacts, *emails.values()]

    def _escalate_email_api(
        self, dc: DiscoveredCompany, contacts: list[Contact]
    ) -> list[Contact]:
        """도메인을 이메일 탐색 API(Hunter→Apollo)에 질의해 이메일을 보강한다(유료).

        정적·헤드리스·OCR 모두 0건일 때만 진입. 키가 있는 제공자만 순차 시도하고, 한
        제공자에서 이메일을 확보하면 다음 제공자 과금/크레딧을 아끼려 조기 종료한다.
        반환 이메일은 :func:`emails_from_text` 로 site 추출과 동일한 role 필터(IR 우선,
        HR·언론 배제. 개인명은 일반으로 채택)를 거친다. 제공자가 없거나 0건이면 유지.
        """
        finders = self._email_finders_list()
        if not finders:  # 키 없음 → no-op(과금 없음).
            return contacts
        cap = max(1, self._settings.email_api_max_results)
        emails: dict[str, Contact] = {}
        for finder in finders:
            if self._budget_blocked():  # 제공자마다 재확인 — 루프 내 예산 초과 차단.
                break
            raw = finder.find_emails(dc.domain, limit=cap)
            self._record_cost(finder.name)  # 유료 호출 1건(제공자별 과금).
            # 제3자 DB 추정치라 다른 escalation 티어(OCR/Vision)와 동일한 낮은 신뢰도.
            for c in emails_from_text(
                " ".join(raw), source_url=finder.source,
                method=ExtractMethod.API, confidence=0.5,
            ):
                emails.setdefault(c.value, c)
            if emails:  # 한 제공자에서 확보 시 다음 제공자 과금/크레딧 회피.
                break
        if not emails:
            return contacts
        log.info("enrich.email_api", domain=dc.domain, emails=len(emails))
        return [*contacts, *emails.values()]

    def _escalate_vision(self, dc: DiscoveredCompany, contacts: list[Contact]) -> list[Contact]:
        """홈페이지 이미지를 Claude Vision 으로 읽어 이메일을 보강한다(유료·최후수단).

        정적·헤드리스·OCR 모두 0건이고 키·플래그가 있을 때만 진입. vision_max_images 로
        과금을 엄격히 제한하고 이미지 확보 시 조기 종료. 실패면 ``contacts`` 유지.
        """
        fetcher = self._client()
        vision = self._vision_obj()
        home = f"https://{dc.domain}"
        try:
            home_html = self._home_html(fetcher, home)
        except Exception as exc:  # 홈 fetch 실패 → 기존 결과 유지.
            log.info("enrich.vision.home_error", domain=dc.domain, err=str(exc))
            return contacts

        img_urls = candidate_images(
            home_html, base_url=home, domain=dc.domain, limit=self._settings.vision_max_images
        )
        emails: dict[str, Contact] = {}
        for url in img_urls:
            media_type = media_type_for(url)
            if media_type is None:  # anthropic 미지원 확장자(bmp 등) → 과금 회피 스킵.
                continue
            if self._budget_blocked():  # 이미지마다 재확인 — 루프 내 예산 초과 차단.
                break
            data = _safe_bytes(fetcher, url)
            if data is None:
                continue
            text = vision.extract_text(data, media_type=media_type)
            self._record_cost("vision")  # Vision 이미지 1장 = 과금 1건.
            for c in emails_from_text(text, source_url=url):
                emails.setdefault(c.value, c)
            if emails:  # 이메일 확보 시 조기 종료(과금 절감).
                break
        if not emails:
            return contacts
        log.info("enrich.vision", domain=dc.domain, emails=len(emails))
        return [*contacts, *emails.values()]

    def _live(self, dc: DiscoveredCompany) -> list[Contact]:
        """정적 BFS 로 홈페이지·IR/문의 페이지를 훑어 연락처를 모은다."""
        fetcher = self._client()
        cap = max(1, self._settings.enrich_max_pages)
        home = f"https://{dc.domain}"
        try:
            home_html = self._home_html(fetcher, home)
        except Exception as exc:
            log.info("enrich.home.error", domain=dc.domain, err=str(exc))
            return []

        links = candidate_links(home_html, base_url=home, domain=dc.domain, limit=cap)
        pages = [home, *links][:cap]
        emails: dict[str, Contact] = {}
        phones: dict[str, Contact] = {}
        form: Contact | None = None
        contact_page: str | None = None  # 폼 미탐지 시 폴백용 문의페이지 URL.

        for url in pages:
            html = home_html if url == home else _safe_get(fetcher, url)
            if html is None:
                continue
            tree = HTMLParser(html or "")  # 페이지당 1회 파싱 — 아래 추출기들이 공유(재파싱 제거).
            for c in extract_emails(html, source_url=url, tree=tree):
                cur = emails.get(c.value)
                if cur is None or c.confidence > cur.confidence:
                    emails[c.value] = c
            for c in extract_phones(html, source_url=url, tree=tree):
                phones.setdefault(c.value, c)
            if form is None:
                form = extract_form(html, page_url=url, tree=tree)
            if contact_page is None and is_contact_page(url, html, tree=tree):
                contact_page = url

        # 문의페이지 힌트는 여기서 폼으로 만들지 않는다 — enrich() 체인 끝(헤드리스/OCR/
        # API/Vision 모두 실패) 최후 폴백에서만 쓴다. 정적 단계에서 폼 슬롯을 채우면 이후
        # 헤드리스가 진짜 JS폼을 못 올리고, 오탐 폴백도 정정 못 한다(아키텍트 MAJOR1).
        self._contact_page = contact_page

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


def _has_form(contacts: list[Contact]) -> bool:
    """문의폼 연락처가 하나라도 있는지(폴백 중복 생성 방지)."""
    return any(c.type is ContactType.FORM for c in contacts)


def _safe_get(fetcher: SupportsFetch, url: str) -> str | None:
    try:
        return fetcher.get_text(url)
    except Exception as exc:  # 개별 페이지 실패는 건너뛴다.
        log.info("enrich.page.error", url=url, err=str(exc))
        return None


def _safe_bytes(fetcher: SupportsFetch, url: str) -> bytes | None:
    try:
        return fetcher.get_bytes(url)
    except Exception as exc:  # 개별 이미지 실패는 건너뛴다.
        log.info("enrich.image.error", url=url, err=str(exc))
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
