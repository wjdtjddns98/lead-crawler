"""보강(enrich) 정적 추출 + Enricher 테스트(네트워크 없음)."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.enrich.enricher import Enricher
from leadcrawler.enrich.extract import (
    candidate_images,
    candidate_links,
    emails_from_text,
    extract_emails,
    extract_form,
    extract_phones,
)
from leadcrawler.models import ContactType, EmailRole, ExtractMethod
from leadcrawler.sources.base import DiscoveredCompany

_HOME = """
<html><body>
  <a href="/investor/ir">IR 투자정보</a>
  <a href="/contact">문의하기</a>
  <a href="/recruit">채용</a>
  <a href="https://other.com/x">외부</a>
  <a href="mailto:ir@acme.co.kr">IR 메일</a>
  <a href="mailto:hr@acme.co.kr">채용문의</a>
  <a href="tel:+82-2-1234-5678">대표전화</a>
  연락: info@acme.co.kr / press@acme.co.kr
</body></html>
"""

_CONTACT = """
<html><body>
  <form action="/contact/submit" id="contact-form">문의 폼 <input name="email"></form>
</body></html>
"""


def test_extract_emails_excludes_hr_and_press() -> None:
    emails = {c.value: c for c in extract_emails(_HOME, source_url="https://acme.co.kr")}
    assert "ir@acme.co.kr" in emails and "info@acme.co.kr" in emails
    # HR/언론은 배제.
    assert "hr@acme.co.kr" not in emails and "press@acme.co.kr" not in emails
    assert emails["ir@acme.co.kr"].role is EmailRole.IR
    # mailto 가 본문보다 신뢰도 높다.
    assert emails["ir@acme.co.kr"].confidence >= emails["info@acme.co.kr"].confidence


def test_extract_phones() -> None:
    phones = extract_phones(_HOME)
    assert any("1234-5678" in p.value for p in phones)
    assert all(p.type is ContactType.PHONE for p in phones)


def test_extract_form_detects_contact_form() -> None:
    form = extract_form(_CONTACT, page_url="https://acme.co.kr/contact")
    assert form is not None
    assert form.type is ContactType.FORM
    assert form.value == "https://acme.co.kr/contact/submit"


def test_candidate_links_prioritizes_ir_and_same_domain() -> None:
    links = candidate_links(_HOME, base_url="https://acme.co.kr", domain="acme.co.kr")
    assert links[0].endswith("/investor/ir")  # IR 우선.
    assert any(u.endswith("/contact") for u in links)
    # 외부 도메인·채용은 후보에서 제외(채용은 contact 힌트 아님, other.com 은 타도메인).
    assert all("other.com" not in u for u in links)


class FakeFetcher:
    def __init__(self, pages: dict[str, str], images: dict[str, bytes] | None = None) -> None:
        self._pages = pages
        self._images = images or {}
        self.calls = 0

    def get_text(self, url: str, *, params=None, headers=None) -> str:
        self.calls += 1
        if url not in self._pages:
            raise KeyError(url)
        return self._pages[url]

    def get_json(self, url, *, params=None, headers=None):  # 미사용
        raise NotImplementedError

    def get_bytes(self, url, *, params=None, headers=None) -> bytes:
        if url not in self._images:
            raise KeyError(url)
        return self._images[url]


def test_extract_handles_empty_and_broken_html() -> None:
    assert extract_emails("") == []
    assert extract_phones("") == []
    assert extract_form("<html>", page_url="https://x.com") is None
    assert candidate_links("", base_url="https://x.com", domain="x.com") == []


def test_enricher_dry_run_is_deterministic() -> None:
    dc = DiscoveredCompany(canonical_key="dom:x.com", name="A", domain="x.com")
    out = Enricher(Settings(dry_run=True)).enrich(dc)
    assert {c.type for c in out} == {ContactType.EMAIL, ContactType.PHONE, ContactType.FORM}
    assert out[0].value == "ir@x.com"


def test_enricher_live_crawls_home_and_candidates() -> None:
    pages = {
        "https://acme.co.kr": _HOME,
        "https://acme.co.kr/investor/ir": '<a href="mailto:invest@acme.co.kr">IR</a>',
        "https://acme.co.kr/contact": _CONTACT,
    }
    settings = Settings(dry_run=False, enrich_max_pages=6)
    dc = DiscoveredCompany(canonical_key="dom:acme.co.kr", name="ACME", domain="acme.co.kr")
    out = Enricher(settings, fetcher=FakeFetcher(pages)).enrich(dc)

    emails = {c.value for c in out if c.type is ContactType.EMAIL}
    assert "ir@acme.co.kr" in emails and "invest@acme.co.kr" in emails
    assert "hr@acme.co.kr" not in emails  # 배제 유지.
    assert any(c.type is ContactType.FORM for c in out)  # contact 페이지 폼.


def test_enricher_live_respects_page_cap() -> None:
    # 후보가 cap 보다 많아도 최대 cap 페이지만 가져온다.
    home = "".join(f'<a href="/investor/p{i}">IR{i}</a>' for i in range(20))
    pages = {"https://acme.co.kr": home}
    pages.update({f"https://acme.co.kr/investor/p{i}": "<html></html>" for i in range(20)})
    fetcher = FakeFetcher(pages)
    settings = Settings(dry_run=False, enrich_max_pages=3)
    dc = DiscoveredCompany(canonical_key="dom:acme.co.kr", name="ACME", domain="acme.co.kr")
    Enricher(settings, fetcher=fetcher).enrich(dc)
    assert fetcher.calls <= 3  # 홈 + 후보 2 = cap.


def test_enricher_form_only_when_no_email() -> None:
    pages = {
        "https://acme.co.kr": '<a href="/contact">문의</a>',
        "https://acme.co.kr/contact": (
            '<form action="/contact/submit" id="contact-form">문의</form>'
        ),
    }
    settings = Settings(dry_run=False)
    dc = DiscoveredCompany(canonical_key="dom:acme.co.kr", name="ACME", domain="acme.co.kr")
    out = Enricher(settings, fetcher=FakeFetcher(pages)).enrich(dc)
    # 이메일은 없고 폼만 — 채택 이메일 없음 + FORM 존재(엑셀 J="사이트 내 문의폼").
    assert not [c for c in out if c.type is ContactType.EMAIL]
    assert any(c.type is ContactType.FORM for c in out)


def test_enricher_live_no_domain_is_empty() -> None:
    dc = DiscoveredCompany(canonical_key="name:kr:x", name="무도메인")
    assert Enricher(Settings(dry_run=False)).enrich(dc) == []


def test_enricher_live_home_failure_returns_empty() -> None:
    settings = Settings(dry_run=False)
    dc = DiscoveredCompany(canonical_key="dom:dead.com", name="죽음", domain="dead.com")
    # 홈페이지 fetch 실패(맵에 없음 → KeyError) → 빈 결과(크래시 없음).
    assert Enricher(settings, fetcher=FakeFetcher({})).enrich(dc) == []


# --- 추출 출처(method) 표기 -------------------------------------------

def test_extract_emails_method_is_settable() -> None:
    out = extract_emails('<a href="mailto:ir@x.com">x</a>', method=ExtractMethod.HEADLESS)
    assert out and out[0].extract_method is ExtractMethod.HEADLESS


# --- 헤드리스 escalation(가짜 렌더러, 브라우저·네트워크 없음) ---------

class FakeRenderer:
    """SupportsRender 더블 — url→HTML(없으면 None=렌더실패) + 호출 기록."""

    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = pages
        self.calls: list[str] = []
        self.closed = False

    def render(self, url: str) -> str | None:
        self.calls.append(url)
        return self._pages.get(url)

    def close(self) -> None:
        self.closed = True


_DC = DiscoveredCompany(canonical_key="dom:acme.co.kr", name="ACME", domain="acme.co.kr")


def test_headless_escalates_when_no_static_email() -> None:
    # 정적: 홈에 IR 링크+전화, IR 페이지엔 이메일 없음 → 정적 이메일 0.
    static = {
        "https://acme.co.kr": '<a href="/investor/ir">IR</a><a href="tel:+82-2-1234-5678">T</a>',
        "https://acme.co.kr/investor/ir": "<html>no email</html>",
    }
    # 렌더: JS 로 mailto 가 주입된 IR 페이지.
    rendered = {
        "https://acme.co.kr": '<a href="/investor/ir">IR</a>',
        "https://acme.co.kr/investor/ir": '<a href="mailto:ir@acme.co.kr">IR</a>',
    }
    settings = Settings(dry_run=False, enrich_headless=True, enrich_max_pages=6)
    renderer = FakeRenderer(rendered)
    out = Enricher(settings, fetcher=FakeFetcher(static), renderer=renderer).enrich(_DC)

    emails = [c for c in out if c.type is ContactType.EMAIL]
    assert {c.value for c in emails} == {"ir@acme.co.kr"}
    assert emails[0].extract_method is ExtractMethod.HEADLESS  # 출처 표기.
    assert any(c.type is ContactType.PHONE for c in out)  # 정적 전화 보존.
    assert renderer.calls  # 렌더러 호출됨.


def test_headless_early_exit_once_email_found() -> None:
    # 정적 이메일 0(링크만) → escalate. 렌더된 홈에 이메일 있으면 후보는 렌더 안 함(§4).
    static = {"https://acme.co.kr": '<a href="/investor/ir">IR</a><a href="/contact">C</a>'}
    rendered = {
        "https://acme.co.kr": '<a href="mailto:ir@acme.co.kr">IR</a><a href="/investor/ir">IR</a>',
        "https://acme.co.kr/investor/ir": '<a href="mailto:other@acme.co.kr">x</a>',
    }
    renderer = FakeRenderer(rendered)
    out = Enricher(
        Settings(dry_run=False, enrich_headless=True), fetcher=FakeFetcher(static), renderer=renderer
    ).enrich(_DC)
    assert renderer.calls == ["https://acme.co.kr"]  # 홈에서 확보 → 후보 렌더 생략.
    assert {c.value for c in out if c.type is ContactType.EMAIL} == {"ir@acme.co.kr"}


def test_headless_off_by_default_does_not_render() -> None:
    static = {"https://acme.co.kr": "<html>no email</html>"}
    renderer = FakeRenderer({})
    out = Enricher(
        Settings(dry_run=False, enrich_headless=False), fetcher=FakeFetcher(static), renderer=renderer
    ).enrich(_DC)
    assert renderer.calls == []  # 기본 off → 렌더 미호출.
    assert not [c for c in out if c.type is ContactType.EMAIL]


def test_headless_skipped_when_static_has_email() -> None:
    static = {"https://acme.co.kr": '<a href="mailto:ir@acme.co.kr">IR</a>'}
    renderer = FakeRenderer({})
    out = Enricher(
        Settings(dry_run=False, enrich_headless=True), fetcher=FakeFetcher(static), renderer=renderer
    ).enrich(_DC)
    assert renderer.calls == []  # 정적 이메일 있으면 escalate 안 함.
    assert any(c.type is ContactType.EMAIL for c in out)


def test_headless_render_failure_keeps_static() -> None:
    static = {"https://acme.co.kr": '<a href="tel:+82-2-1234-5678">T</a>'}
    renderer = FakeRenderer({})  # 모든 render → None(미설치/실패 시뮬).
    out = Enricher(
        Settings(dry_run=False, enrich_headless=True), fetcher=FakeFetcher(static), renderer=renderer
    ).enrich(_DC)
    # 렌더 실패 → 정적 결과(전화) 유지, 크래시 없음.
    assert any(c.type is ContactType.PHONE for c in out)
    assert not [c for c in out if c.type is ContactType.EMAIL]
    assert renderer.calls == ["https://acme.co.kr"]  # 홈 렌더 1회 시도 후 폴백.


# --- OCR escalation(가짜 OCR·이미지, 바이너리·네트워크 없음) ----------

def test_candidate_images_prioritizes_mail_hints_and_caps() -> None:
    html = (
        '<img src="/logo.png">'
        '<img src="/img/email-address.png" alt="문의 이메일">'
        '<img src="data:image/png;base64,xxx">'  # data: 제외.
        '<img src="/banner.svg">'  # 비이미지 확장자 제외(.svg 미포함).
        '<img src="/contact.jpg">'
    )
    imgs = candidate_images(html, base_url="https://acme.co.kr", limit=5)
    # 메일 힌트(email-address.png, contact.jpg)가 logo.png 보다 앞.
    assert imgs[0].endswith("/img/email-address.png")
    assert "data:" not in " ".join(imgs) and not any(u.endswith(".svg") for u in imgs)
    assert any(u.endswith("/contact.jpg") for u in imgs)


def test_emails_from_text_filters_roles() -> None:
    text = "문의: ir@acme.co.kr 채용: hr@acme.co.kr 보도: press@acme.co.kr"
    out = emails_from_text(text, source_url="img://x")
    assert {c.value for c in out} == {"ir@acme.co.kr"}  # HR/언론 배제.
    assert out[0].extract_method is ExtractMethod.OCR_VISION


class FakeOcr:
    """SupportsOcr 더블 — 고정 텍스트 반환 + 호출 기록."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[bytes] = []

    def image_to_text(self, image: bytes) -> str:
        self.calls.append(image)
        return self._text


def test_ocr_escalates_when_no_email() -> None:
    static = {"https://acme.co.kr": '<a href="tel:+82-2-1234-5678">T</a><img src="/m.png" alt="email">'}
    images = {"https://acme.co.kr/m.png": b"PNGBYTES"}
    ocr = FakeOcr("연락: ir@acme.co.kr")
    out = Enricher(
        Settings(dry_run=False, enrich_ocr=True),
        fetcher=FakeFetcher(static, images),
        ocr=ocr,
    ).enrich(_DC)
    emails = [c for c in out if c.type is ContactType.EMAIL]
    assert {c.value for c in emails} == {"ir@acme.co.kr"}
    assert emails[0].extract_method is ExtractMethod.OCR_VISION
    assert any(c.type is ContactType.PHONE for c in out)  # 정적 전화 보존.
    assert ocr.calls == [b"PNGBYTES"]


def test_ocr_off_by_default_does_not_run() -> None:
    static = {"https://acme.co.kr": '<img src="/m.png" alt="email">'}
    ocr = FakeOcr("ir@acme.co.kr")
    out = Enricher(
        Settings(dry_run=False, enrich_ocr=False),
        fetcher=FakeFetcher(static, {"https://acme.co.kr/m.png": b"X"}),
        ocr=ocr,
    ).enrich(_DC)
    assert ocr.calls == [] and not [c for c in out if c.type is ContactType.EMAIL]


def test_ocr_skipped_when_static_has_email() -> None:
    static = {"https://acme.co.kr": '<a href="mailto:ir@acme.co.kr">IR</a><img src="/m.png" alt="email">'}
    ocr = FakeOcr("other@acme.co.kr")
    out = Enricher(
        Settings(dry_run=False, enrich_ocr=True),
        fetcher=FakeFetcher(static, {"https://acme.co.kr/m.png": b"X"}),
        ocr=ocr,
    ).enrich(_DC)
    assert ocr.calls == []  # 정적 이메일 있으면 OCR 미실행.
    assert {c.value for c in out if c.type is ContactType.EMAIL} == {"ir@acme.co.kr"}


def test_ocr_no_email_found_keeps_contacts() -> None:
    static = {"https://acme.co.kr": '<a href="tel:+82-2-1234-5678">T</a><img src="/m.png" alt="email">'}
    ocr = FakeOcr("이메일 없는 텍스트")  # OCR 결과에 이메일 없음.
    out = Enricher(
        Settings(dry_run=False, enrich_ocr=True),
        fetcher=FakeFetcher(static, {"https://acme.co.kr/m.png": b"X"}),
        ocr=ocr,
    ).enrich(_DC)
    assert not [c for c in out if c.type is ContactType.EMAIL]
    assert any(c.type is ContactType.PHONE for c in out)  # 기존 결과 유지.


# --- Vision escalation(가짜 Vision, API·네트워크 없음) -----------------

def test_media_type_for_maps_extensions() -> None:
    from leadcrawler.enrich.vision import media_type_for

    assert media_type_for("https://x/a.JPG") == "image/jpeg"
    assert media_type_for("https://x/a.png?v=2") == "image/png"
    assert media_type_for("https://x/a.webp") == "image/webp"
    assert media_type_for("https://x/a.bmp") is None  # anthropic 미지원 → None.
    assert media_type_for("https://x/a.unknown") is None


def test_vision_skips_oversized_image_before_api_call() -> None:
    from leadcrawler.enrich.vision import ClaudeVision

    big = b"x" * (4 * 1024 * 1024 + 1)
    # 4MB 초과 → API 호출(과금) 없이 빈 문자열(anthropic import 도 안 함).
    assert ClaudeVision("k", model="m").extract_text(big) == ""


class FakeVision:
    """SupportsVision 더블 — 고정 텍스트 반환 + 호출 기록."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[tuple[bytes, str]] = []

    def extract_text(self, image: bytes, *, media_type: str = "image/png") -> str:
        self.calls.append((image, media_type))
        return self._text


def _vision_settings(**over: object) -> Settings:
    """Vision 라이브 설정(키 + 플래그) — over 로 추가 조정."""
    return Settings(dry_run=False, enrich_vision=True, anthropic_api_key="k", **over)


def test_vision_escalates_when_no_email() -> None:
    static = {"https://acme.co.kr": '<a href="tel:+82-2-1234-5678">T</a><img src="/m.jpg" alt="email">'}
    images = {"https://acme.co.kr/m.jpg": b"JPGBYTES"}
    vision = FakeVision("연락: ir@acme.co.kr")
    out = Enricher(
        _vision_settings(), fetcher=FakeFetcher(static, images), vision=vision
    ).enrich(_DC)
    emails = [c for c in out if c.type is ContactType.EMAIL]
    assert {c.value for c in emails} == {"ir@acme.co.kr"}
    assert emails[0].extract_method is ExtractMethod.OCR_VISION
    assert any(c.type is ContactType.PHONE for c in out)  # 정적 전화 보존.
    assert vision.calls and vision.calls[0][1] == "image/jpeg"  # media_type 추정.


def test_vision_off_by_default_does_not_run() -> None:
    static = {"https://acme.co.kr": '<img src="/m.jpg" alt="email">'}
    vision = FakeVision("ir@acme.co.kr")
    out = Enricher(
        Settings(dry_run=False, enrich_vision=False, anthropic_api_key="k"),
        fetcher=FakeFetcher(static, {"https://acme.co.kr/m.jpg": b"X"}),
        vision=vision,
    ).enrich(_DC)
    assert vision.calls == [] and not [c for c in out if c.type is ContactType.EMAIL]


def test_vision_skipped_without_api_key() -> None:
    static = {"https://acme.co.kr": '<img src="/m.jpg" alt="email">'}
    vision = FakeVision("ir@acme.co.kr")
    # enrich_vision on 이지만 키 없음 → 호출 안 함(과금 보호).
    out = Enricher(
        Settings(dry_run=False, enrich_vision=True, anthropic_api_key=""),
        fetcher=FakeFetcher(static, {"https://acme.co.kr/m.jpg": b"X"}),
        vision=vision,
    ).enrich(_DC)
    assert vision.calls == [] and not [c for c in out if c.type is ContactType.EMAIL]


def test_vision_skipped_when_email_already_found() -> None:
    static = {"https://acme.co.kr": '<a href="mailto:ir@acme.co.kr">IR</a><img src="/m.jpg" alt="email">'}
    vision = FakeVision("other@acme.co.kr")
    out = Enricher(
        _vision_settings(), fetcher=FakeFetcher(static, {"https://acme.co.kr/m.jpg": b"X"}), vision=vision
    ).enrich(_DC)
    assert vision.calls == []  # 정적 이메일 있으면 Vision 미실행.
    assert {c.value for c in out if c.type is ContactType.EMAIL} == {"ir@acme.co.kr"}


def test_vision_skips_unsupported_media_type() -> None:
    # .bmp 는 anthropic 미지원 → 후보지만 Vision 호출 안 함(과금 회피).
    static = {"https://acme.co.kr": '<a href="tel:+82-2-1234-5678">T</a><img src="/m.bmp" alt="email">'}
    vision = FakeVision("ir@acme.co.kr")
    out = Enricher(
        _vision_settings(), fetcher=FakeFetcher(static, {"https://acme.co.kr/m.bmp": b"X"}), vision=vision
    ).enrich(_DC)
    assert vision.calls == []
    assert not [c for c in out if c.type is ContactType.EMAIL]


def test_vision_no_email_keeps_contacts() -> None:
    static = {"https://acme.co.kr": '<a href="tel:+82-2-1234-5678">T</a><img src="/m.jpg" alt="email">'}
    vision = FakeVision("이메일 없음")
    out = Enricher(
        _vision_settings(), fetcher=FakeFetcher(static, {"https://acme.co.kr/m.jpg": b"X"}), vision=vision
    ).enrich(_DC)
    assert not [c for c in out if c.type is ContactType.EMAIL]
    assert any(c.type is ContactType.PHONE for c in out)
