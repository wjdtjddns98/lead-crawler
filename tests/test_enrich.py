"""보강(enrich) 정적 추출 + Enricher 테스트(네트워크 없음)."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.enrich.enricher import Enricher
from leadcrawler.enrich.extract import (
    candidate_links,
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
    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = pages
        self.calls = 0

    def get_text(self, url: str, *, params=None, headers=None) -> str:
        self.calls += 1
        if url not in self._pages:
            raise KeyError(url)
        return self._pages[url]

    def get_json(self, url, *, params=None, headers=None):  # 미사용
        raise NotImplementedError

    def get_bytes(self, url, *, params=None, headers=None):  # 미사용
        raise NotImplementedError


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
