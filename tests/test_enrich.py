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
    is_contact_page,
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


def test_extract_emails_rejects_asset_filenames_and_placeholders() -> None:
    # 이미지 파일명(@2x...jpg)·예시 도메인(example.com)이 이메일로 새지 않는다.
    html = (
        "<body>진짜 ir@good.com · 가짜이미지 banner_homepage-texture-1@2x-992x379.jpg"
        " · 플레이스홀더 address@example.com</body>"
    )
    emails = {c.value for c in extract_emails(html)}
    assert "ir@good.com" in emails
    assert not any(".jpg" in e for e in emails)
    assert "address@example.com" not in emails


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


def test_shared_tree_matches_html_parse() -> None:
    """tree 공유 호출 == html 파싱 호출(추출물·순서 동일) — 재파싱 제거가 결과를 안 바꾼다."""
    from selectolax.parser import HTMLParser

    url = "https://acme.co.kr"
    tree = HTMLParser(_HOME)
    assert [c.value for c in extract_emails(_HOME, source_url=url)] == [
        c.value for c in extract_emails(_HOME, source_url=url, tree=tree)
    ]
    assert [c.value for c in extract_phones(_HOME)] == [
        c.value for c in extract_phones(_HOME, tree=tree)
    ]
    assert candidate_links(_HOME, base_url=url, domain="acme.co.kr") == candidate_links(
        _HOME, base_url=url, domain="acme.co.kr", tree=tree
    )

    ctree = HTMLParser(_CONTACT)
    f_html = extract_form(_CONTACT, page_url=url)
    f_tree = extract_form(_CONTACT, page_url=url, tree=ctree)
    assert (f_html.value if f_html else None) == (f_tree.value if f_tree else None)

    # is_contact_page title 분기도 tree 로 동일 판정.
    contact_html = "<html><head><title>Contact Us</title></head><body></body></html>"
    assert is_contact_page("https://x.com/page", contact_html) is True
    assert (
        is_contact_page("https://x.com/page", contact_html, tree=HTMLParser(contact_html)) is True
    )

    img_html = '<img src="/mail-icon.png" alt="email"><img src="/logo.png">'
    assert candidate_images(img_html, base_url=url) == candidate_images(
        img_html, base_url=url, tree=HTMLParser(img_html)
    )


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


# ── 이메일 추출 강화(난독화·CF보호·원문스캔·프로빙·www폴백) ─────────────────


def _cf_encode(email: str, key: int = 0x23) -> str:
    """테스트용 Cloudflare email-protection 인코더(첫 바이트=XOR 키)."""
    return bytes([key, *(b ^ key for b in email.encode())]).hex()


def test_extract_emails_decodes_cloudflare_protection() -> None:
    # data-cfemail 속성과 /cdn-cgi/l/email-protection# 링크 둘 다 복호한다.
    html = (
        f'<span data-cfemail="{_cf_encode("ir@acme.co.kr")}">[email protected]</span>'
        f'<a href="/cdn-cgi/l/email-protection#{_cf_encode("info@acme.co.kr", 0x7a)}">메일</a>'
    )
    emails = {c.value: c for c in extract_emails(html, source_url="https://acme.co.kr")}
    assert set(emails) == {"ir@acme.co.kr", "info@acme.co.kr"}
    assert emails["ir@acme.co.kr"].confidence >= 0.9  # 의도적 게시 — mailto 급 신뢰.


def test_extract_emails_ignores_broken_cfemail() -> None:
    # 깨진 페이로드(홀수 hex·비hex·빈값)는 조용히 무시(크래시 없음).
    html = '<span data-cfemail="zz">x</span><span data-cfemail="a">y</span>'
    assert extract_emails(html) == []


def test_extract_emails_deobfuscates_at_dot() -> None:
    html = "<body>문의: info (at) acme (dot) co (dot) kr / ir[at]acme[dot]com</body>"
    emails = {c.value for c in extract_emails(html)}
    assert emails == {"info@acme.co.kr", "ir@acme.com"}


def test_extract_emails_from_raw_html_attributes_and_jsonld() -> None:
    # tree.text() 에 안 잡히는 속성·JSON-LD 속 이메일을 원문 스캔(저신뢰)으로 잡는다.
    html = (
        '<a href="#" data-email="contact@acme.co.kr">Contact</a>'
        '<script type="application/ld+json">{"@type":"Organization",'
        '"email":"help@acme.co.kr"}</script>'
    )
    emails = {c.value: c for c in extract_emails(html)}
    assert {"contact@acme.co.kr", "help@acme.co.kr"} <= set(emails)
    assert emails["contact@acme.co.kr"].confidence <= 0.4  # 원문 스캔은 저신뢰.


def test_extract_emails_rejects_sentry_dsn_subdomain() -> None:
    # 원문 스캔이 여는 SDK 키 오탐 — 플레이스홀더 서픽스 매칭으로 차단.
    html = '<script>init("https://abc123@o12345.ingest.sentry.io/678")</script>'
    assert extract_emails(html) == []


def test_emails_from_text_deobfuscates() -> None:
    out = emails_from_text("문의 ir[at]acme[dot]co[dot]kr")
    assert {c.value for c in out} == {"ir@acme.co.kr"}


def test_enricher_live_falls_back_to_www() -> None:
    # naked 도메인 미해석(fetch 실패) → https://www. 폴백으로 이메일 확보.
    pages = {"https://www.acme.co.kr": '<a href="mailto:ir@acme.co.kr">IR</a>'}
    out = Enricher(Settings(dry_run=False), fetcher=FakeFetcher(pages)).enrich(_DC)
    assert {c.value for c in out if c.type is ContactType.EMAIL} == {"ir@acme.co.kr"}


def test_enricher_live_probes_common_paths_when_no_links() -> None:
    # JS 렌더 내비(정적 앵커 0개) → /contact 관용 경로 프로빙으로 이메일 확보.
    pages = {
        "https://acme.co.kr": "<html><body>JS nav</body></html>",
        "https://acme.co.kr/contact": '<a href="mailto:info@acme.co.kr">메일</a>',
    }
    out = Enricher(Settings(dry_run=False), fetcher=FakeFetcher(pages)).enrich(_DC)
    assert {c.value for c in out if c.type is ContactType.EMAIL} == {"info@acme.co.kr"}


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


# --- Email Finder API escalation(Hunter/Apollo, 가짜 주입, 네트워크 없음) -------

# 정적으로 이메일이 안 나오는 홈(전화만) — escalation 진입 조건을 만든다.
_NO_EMAIL_HOME = {"https://acme.co.kr": '<a href="tel:+82-2-1234-5678">대표</a>'}


class FakeFinder:
    """SupportsEmailFinder 더블 — 고정 이메일 반환 + 호출 기록."""

    def __init__(self, name: str, emails: list[str]) -> None:
        self.name = name
        self.source = f"https://{name}.test"
        self._emails = emails
        self.calls: list[tuple[str, int]] = []

    def find_emails(self, domain: str, *, limit: int = 5) -> list[str]:
        self.calls.append((domain, limit))
        return self._emails


def _email_api_settings(**over: object) -> Settings:
    """이메일 API escalation 라이브 설정(플래그 on) — over 로 추가 조정."""
    return Settings(dry_run=False, enrich_email_api=True, **over)


def test_email_api_escalates_when_no_email() -> None:
    finder = FakeFinder("hunter", ["ir@acme.co.kr"])
    out = Enricher(
        _email_api_settings(), fetcher=FakeFetcher(_NO_EMAIL_HOME), email_finders=[finder]
    ).enrich(_DC)
    emails = [c for c in out if c.type is ContactType.EMAIL]
    assert {c.value for c in emails} == {"ir@acme.co.kr"}
    assert emails[0].extract_method is ExtractMethod.API
    assert emails[0].role is EmailRole.IR
    assert any(c.type is ContactType.PHONE for c in out)  # 정적 전화 보존.
    assert finder.calls == [("acme.co.kr", 5)]


def test_email_api_off_by_default_does_not_run() -> None:
    finder = FakeFinder("hunter", ["ir@acme.co.kr"])
    out = Enricher(
        Settings(dry_run=False, enrich_email_api=False),
        fetcher=FakeFetcher(_NO_EMAIL_HOME),
        email_finders=[finder],
    ).enrich(_DC)
    assert finder.calls == [] and not [c for c in out if c.type is ContactType.EMAIL]


def test_email_api_skipped_when_email_already_found() -> None:
    static = {"https://acme.co.kr": '<a href="mailto:ir@acme.co.kr">IR</a>'}
    finder = FakeFinder("hunter", ["other@acme.co.kr"])
    out = Enricher(
        _email_api_settings(), fetcher=FakeFetcher(static), email_finders=[finder]
    ).enrich(_DC)
    assert finder.calls == []  # 정적 이메일 있으면 API 미실행.
    assert {c.value for c in out if c.type is ContactType.EMAIL} == {"ir@acme.co.kr"}


def test_email_api_filters_hr_and_press() -> None:
    # HR/언론은 배제, IR·개인명(general)은 채택 — site 추출과 동일 정책.
    finder = FakeFinder(
        "hunter", ["ir@acme.co.kr", "hr@acme.co.kr", "press@acme.co.kr", "jane.doe@acme.co.kr"]
    )
    out = Enricher(
        _email_api_settings(), fetcher=FakeFetcher(_NO_EMAIL_HOME), email_finders=[finder]
    ).enrich(_DC)
    vals = {c.value for c in out if c.type is ContactType.EMAIL}
    assert vals == {"ir@acme.co.kr", "jane.doe@acme.co.kr"}


def test_email_api_tries_next_provider_until_hit() -> None:
    empty = FakeFinder("hunter", [])  # 1순위 0건 → 2순위 시도.
    apollo = FakeFinder("apollo", ["info@acme.co.kr"])
    out = Enricher(
        _email_api_settings(), fetcher=FakeFetcher(_NO_EMAIL_HOME),
        email_finders=[empty, apollo],
    ).enrich(_DC)
    assert empty.calls and apollo.calls  # 둘 다 시도.
    assert {c.value for c in out if c.type is ContactType.EMAIL} == {"info@acme.co.kr"}


def test_email_api_early_exit_skips_next_provider() -> None:
    hunter = FakeFinder("hunter", ["ir@acme.co.kr"])  # 1순위에서 확보.
    apollo = FakeFinder("apollo", ["info@acme.co.kr"])
    Enricher(
        _email_api_settings(), fetcher=FakeFetcher(_NO_EMAIL_HOME),
        email_finders=[hunter, apollo],
    ).enrich(_DC)
    assert hunter.calls and apollo.calls == []  # 1순위 확보 시 2순위 과금 회피.


def test_email_api_filtered_empty_provider_falls_through() -> None:
    # 1순위가 HR/언론만 반환(전량 필터) → 채택 0건이라 2순위로 진행.
    hronly = FakeFinder("hunter", ["hr@acme.co.kr", "press@acme.co.kr"])
    apollo = FakeFinder("apollo", ["ir@acme.co.kr"])
    out = Enricher(
        _email_api_settings(), fetcher=FakeFetcher(_NO_EMAIL_HOME),
        email_finders=[hronly, apollo],
    ).enrich(_DC)
    assert hronly.calls and apollo.calls  # 필터로 0건이면 다음 제공자 시도.
    assert {c.value for c in out if c.type is ContactType.EMAIL} == {"ir@acme.co.kr"}


def test_email_api_no_finders_keeps_contacts() -> None:
    # 플래그 on 이지만 키 없음(주입도 없음) → 제공자 0개 → no-op(전화 유지).
    out = Enricher(_email_api_settings(), fetcher=FakeFetcher(_NO_EMAIL_HOME)).enrich(_DC)
    assert not [c for c in out if c.type is ContactType.EMAIL]
    assert any(c.type is ContactType.PHONE for c in out)


def test_email_api_no_email_keeps_contacts() -> None:
    finder = FakeFinder("hunter", ["not-an-email", ""])  # 유효 이메일 0건.
    out = Enricher(
        _email_api_settings(), fetcher=FakeFetcher(_NO_EMAIL_HOME), email_finders=[finder]
    ).enrich(_DC)
    assert not [c for c in out if c.type is ContactType.EMAIL]
    assert any(c.type is ContactType.PHONE for c in out)


# --- 제공자 단위(가짜 페처, API·네트워크 없음) ---------------------------------

class FakeApiFetcher:
    """get_json/post_json 더블 — 고정 응답 반환 + 호출 인자 기록."""

    def __init__(self, json_resp: object) -> None:
        self._resp = json_resp
        self.get_params: dict | None = None
        self.post_json_body: object = None

    def get_json(self, url, *, params=None, headers=None):
        self.get_params = params
        return self._resp

    def post_json(self, url, *, json=None, params=None, headers=None):
        self.post_json_body = json
        return self._resp


def test_hunter_finder_parses_and_requests_generic() -> None:
    from leadcrawler.enrich.emailapi import HunterFinder

    resp = {"data": {"emails": [{"value": "ir@acme.co.kr"}, {"value": "info@acme.co.kr"}]}}
    fetcher = FakeApiFetcher(resp)
    out = HunterFinder("k", fetcher=fetcher).find_emails("acme.co.kr", limit=3)
    assert out == ["ir@acme.co.kr", "info@acme.co.kr"]
    assert fetcher.get_params["type"] == "generic"  # 개인 이메일 제외 요청.
    assert fetcher.get_params["domain"] == "acme.co.kr"


def test_apollo_finder_skips_locked_placeholder() -> None:
    from leadcrawler.enrich.emailapi import ApolloFinder

    resp = {"people": [
        {"email": "ir@acme.co.kr"},
        {"email": "email_not_unlocked@domain.com"},  # 미해제 자리표시자.
        {"email": ""},
    ]}
    fetcher = FakeApiFetcher(resp)
    out = ApolloFinder("k", fetcher=fetcher).find_emails("acme.co.kr", limit=5)
    assert out == ["ir@acme.co.kr"]
    assert fetcher.post_json_body["q_organization_domains"] == "acme.co.kr"


def test_email_finder_graceful_on_error() -> None:
    from leadcrawler.enrich.emailapi import HunterFinder

    class Boom:
        def get_json(self, *a, **k):
            raise RuntimeError("api down")

    assert HunterFinder("k", fetcher=Boom()).find_emails("acme.co.kr") == []


# ── 문의폼 탐지 강화(recall + precision) ────────────────────────────────────


def test_extract_form_broadened_multilingual_keywords() -> None:
    # 확대 키워드(한/영/일)가 메시지폼으로 각각 탐지된다.
    for kw in ("상담신청", "견적문의", "고객센터", "support", "enquiry", "お問い合わせ"):
        html = f'<form action="/s" id="x">{kw}<textarea name="msg"></textarea></form>'
        form = extract_form(html, page_url="https://x.com/p")
        assert form is not None and form.type is ContactType.FORM, kw
        assert form.value == "https://x.com/s", kw


def test_extract_form_detects_iframe_embed_providers() -> None:
    cases = {
        "google": '<iframe src="https://docs.google.com/forms/d/e/AB/viewform"></iframe>',
        "typeform": '<iframe src="https://acme.typeform.com/to/abc"></iframe>',
        "naver": '<iframe src="https://form.naver.com/response/xyz"></iframe>',
    }
    for name, html in cases.items():
        form = extract_form(html, page_url="https://x.com/contact")
        assert form is not None, name
        assert form.type is ContactType.FORM and form.value.startswith("http"), name
        assert form.confidence >= 0.7, name  # 임베드는 고신뢰.


def test_extract_form_excludes_search_login_newsletter() -> None:
    # contact/문의 글자가 있어도 검색·로그인·뉴스레터 폼은 문의폼이 아니다.
    search = '<form role="search" id="contact" action="/s">문의 검색<input type="search"></form>'
    login = '<form action="/login" id="contact">문의<input type="password"></form>'
    news = '<form class="newsletter" action="/sub">문의 구독<input type="email" name="email"></form>'
    for html in (search, login, news):
        assert extract_form(html, page_url="https://x.com/p") is None


def test_extract_form_prefers_message_textarea_form() -> None:
    # 키워드만 맞는 폼보다 textarea(메시지) 있는 진짜 문의폼을 우선 선택한다.
    html = (
        '<form action="/a" id="contact">문의</form>'
        '<form action="/b" id="contact2">문의<textarea name="msg"></textarea></form>'
    )
    form = extract_form(html, page_url="https://x.com/p")
    assert form is not None and form.value == "https://x.com/b"
    assert form.confidence >= 0.6


def test_is_contact_page_by_path_and_title() -> None:
    assert is_contact_page("https://x.com/contact")
    assert is_contact_page("https://x.com/contact-us")
    assert is_contact_page("https://x.com/inquiry")
    assert is_contact_page("https://x.com/문의")
    assert is_contact_page("https://x.com/page", "<title>고객문의</title>")
    assert is_contact_page("https://x.com/page", "<h1>온라인 문의</h1>")
    # 문의와 무관한 페이지는 False.
    assert not is_contact_page("https://x.com/about", "<title>회사소개</title>")


def test_is_contact_page_path_is_segment_not_substring() -> None:
    # 좁힌 경로 매칭(아키텍트 MAJOR3) — /support·/customer 등 광역 단어는 문의페이지 아님.
    assert not is_contact_page("https://x.com/support")
    assert not is_contact_page("https://x.com/support-center")
    assert not is_contact_page("https://x.com/customer/notice")
    assert not is_contact_page("https://x.com/contacts-list")  # 'contacts' 세그먼트≠contact


def test_extract_form_iframe_ignores_provider_name_in_query() -> None:
    # 호스트 앵커링(아키텍트 MINOR4) — 쿼리/경로에 제공자명만 든 광고 iframe 은 폼 아님.
    html = '<iframe src="https://ads.example.com/t?utm_source=typeform.com"></iframe>'
    assert extract_form(html, page_url="https://x.com/p") is None


def test_enricher_fallback_to_contact_page_when_no_static_form() -> None:
    # 정적 <form> 없는(=JS 렌더) 문의페이지를 폼 미탐지 시 낮은 신뢰도 폼으로 폴백 채택.
    pages = {
        "https://acme.co.kr": '<a href="/contact">문의하기</a>',
        "https://acme.co.kr/contact": "<html><body>아래 양식을 작성하세요(JS 폼)</body></html>",
    }
    settings = Settings(dry_run=False)
    dc = DiscoveredCompany(canonical_key="dom:acme.co.kr", name="ACME", domain="acme.co.kr")
    out = Enricher(settings, fetcher=FakeFetcher(pages)).enrich(dc)
    forms = [c for c in out if c.type is ContactType.FORM]
    assert not [c for c in out if c.type is ContactType.EMAIL]
    assert forms and forms[0].value == "https://acme.co.kr/contact"
    assert forms[0].confidence <= 0.3  # 폴백은 낮은 신뢰.


def test_enricher_no_form_fallback_when_email_present() -> None:
    # 이메일이 있으면 문의페이지 폴백 폼을 만들지 않는다(폼은 이메일 없을 때만).
    pages = {
        "https://acme.co.kr": '<a href="/contact">문의</a> <a href="mailto:ir@acme.co.kr">IR</a>',
        "https://acme.co.kr/contact": "<html><body>문의 안내(폼 없음)</body></html>",
    }
    settings = Settings(dry_run=False)
    dc = DiscoveredCompany(canonical_key="dom:acme.co.kr", name="ACME", domain="acme.co.kr")
    out = Enricher(settings, fetcher=FakeFetcher(pages)).enrich(dc)
    assert any(c.value == "ir@acme.co.kr" for c in out)
    assert not [c for c in out if c.type is ContactType.FORM]  # 폴백 없음.
