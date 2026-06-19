"""정적 HTML 추출 — 이메일·전화·문의폼 + IR/contact 후보 링크.

네트워크 없이 순수 함수로 동작(테스트 용이). 헤드리스/OCR/비전 단계는 후속.
이메일 role 분류·채택은 :mod:`leadcrawler.emailrules` 를 재사용한다.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from ..dedup import normalize_domain
from ..emailrules import classify_role, is_accepted
from ..models import Contact, ContactType, ExtractMethod

# 이메일 추출 정규식(TLD 길이 상한으로 백트래킹 억제). 전화는 tel: 링크만 신뢰.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}")
# 신뢰불가 페이지의 본문 정규식 스캔 길이 상한(ReDoS/대용량 CPU 방지).
_MAX_TEXT_SCAN = 200_000

# IR/문의 후보 페이지를 가리키는 링크 키워드(BFS 우선순위).
_IR_HINTS = ("investor", "ir", "투자", "투자자")
_CONTACT_HINTS = ("contact", "inquiry", "문의", "고객", "about", "회사소개", "company")

# 이미지/문서 등 따라가지 않을 확장자.
_SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".doc", ".docx", ".xls")

# OCR 대상 이미지 확장자.
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
# 이메일이 담겼을 가능성이 높은 이미지 힌트(src/alt) — 우선 OCR.
_MAIL_IMG_HINTS = ("mail", "email", "contact", "이메일", "메일", "문의")


def extract_emails(
    html: str, *, source_url: str = "", method: ExtractMethod = ExtractMethod.STATIC
) -> list[Contact]:
    """mailto 링크 + 본문 텍스트에서 이메일을 추출(HR/언론 배제).

    ``method`` 로 추출 출처를 표기한다(정적/헤드리스/OCR 등 — 신뢰도 추적용).
    """
    tree = HTMLParser(html or "")
    found: dict[str, Contact] = {}

    def _add(addr: str, confidence: float) -> None:
        addr = addr.strip().strip(".").lower()
        if not addr or addr in found:
            return
        role = classify_role(addr)
        if not is_accepted(role):  # HR/언론/개인 배제.
            return
        found[addr] = Contact(
            type=ContactType.EMAIL,
            value=addr,
            role=role,
            extract_method=method,
            source_url=source_url or None,
            confidence=confidence,
        )

    for node in tree.css("a[href^='mailto:']"):
        href = node.attributes.get("href") or ""
        addr = href[len("mailto:"):].split("?", 1)[0]
        for m in _EMAIL_RE.findall(addr):
            _add(m, 0.9)  # mailto 는 신뢰도 높음.

    # 마크업이 아닌 추출 텍스트만(속성/스크립트 오탐 회피), 길이 상한 적용.
    text = (tree.text() or "")[:_MAX_TEXT_SCAN]
    for m in _EMAIL_RE.findall(text):
        _add(m, 0.6)

    return list(found.values())


def emails_from_text(
    text: str,
    *,
    source_url: str = "",
    method: ExtractMethod = ExtractMethod.OCR_VISION,
    confidence: float = 0.5,
) -> list[Contact]:
    """평문 텍스트(예: OCR/Vision 결과)에서 이메일을 추출(HR/언론 배제).

    HTML 이 아닌 순수 문자열용 — ``extract_emails`` 와 동일한 role 필터·정규화를 쓴다.
    OCR 은 오독 가능성이 있어 기본 신뢰도를 낮게 둔다.
    """
    found: dict[str, Contact] = {}
    for m in _EMAIL_RE.findall((text or "")[:_MAX_TEXT_SCAN]):
        addr = m.strip().strip(".").lower()
        if addr in found:
            continue
        role = classify_role(addr)
        if not is_accepted(role):  # HR/언론/개인 배제.
            continue
        found[addr] = Contact(
            type=ContactType.EMAIL,
            value=addr,
            role=role,
            extract_method=method,
            source_url=source_url or None,
            confidence=confidence,
        )
    return list(found.values())


def extract_phones(html: str, *, source_url: str = "") -> list[Contact]:
    """tel 링크 + 본문에서 전화번호를 추출(노이즈 제거)."""
    tree = HTMLParser(html or "")
    found: dict[str, Contact] = {}

    def _add(raw: str, confidence: float) -> None:
        digits = re.sub(r"\D", "", raw)
        if not (8 <= len(digits) <= 15) or digits in found:
            return
        found[digits] = Contact(
            type=ContactType.PHONE,
            value=raw.strip(),
            extract_method=ExtractMethod.STATIC,
            source_url=source_url or None,
            confidence=confidence,
        )

    for node in tree.css("a[href^='tel:']"):
        href = (node.attributes.get("href") or "")[len("tel:"):]
        _add(href, 0.85)

    return list(found.values())


def extract_form(
    html: str, *, page_url: str, method: ExtractMethod = ExtractMethod.STATIC
) -> Contact | None:
    """문의 성격의 <form> 이 있으면 폼 URL 연락처를 만든다(이메일 없을 때 폴백용)."""
    tree = HTMLParser(html or "")
    for form in tree.css("form"):
        haystack = " ".join(
            (form.attributes.get(a) or "") for a in ("action", "id", "class", "name")
        ).lower()
        text = (form.text() or "").lower()
        if any(h in haystack or h in text for h in ("contact", "inquiry", "문의", "1:1")):
            action = form.attributes.get("action") or ""
            url = urljoin(page_url, action) if action else page_url
            return Contact(
                type=ContactType.FORM,
                value=url,
                extract_method=method,
                source_url=page_url,
                confidence=0.5,
            )
    return None


def candidate_links(
    html: str, *, base_url: str, domain: str | None, limit: int = 20
) -> list[str]:
    """홈페이지에서 IR/문의 후보 내부 링크를 우선순위 순으로 추린다(동일 도메인).

    적대적 페이지의 과도한 앵커 열거를 막기 위해 ``limit`` 개 모으면 조기 종료한다.
    """
    tree = HTMLParser(html or "")
    ir: list[str] = []
    contact: list[str] = []
    seen: set[str] = set()
    for node in tree.css("a[href]"):
        if len(ir) + len(contact) >= limit:
            break
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href)
        if urlparse(url).scheme not in ("http", "https"):
            continue
        if url.lower().endswith(_SKIP_EXT) or url in seen:
            continue
        # 동일 등록도메인만 따라간다(외부 이탈 방지).
        if domain and normalize_domain(url) != normalize_domain(domain):
            continue
        seen.add(url)
        hint = (href + " " + (node.text() or "")).lower()
        if any(h in hint for h in _IR_HINTS):
            ir.append(url)
        elif any(h in hint for h in _CONTACT_HINTS):
            contact.append(url)
    return ir + contact  # IR 우선.


def candidate_images(
    html: str, *, base_url: str, domain: str | None = None, limit: int = 5
) -> list[str]:
    """OCR 대상 이미지 URL 을 추리되, 이메일 가능성 높은 것(src/alt 힌트)을 우선한다.

    스팸회피로 이메일을 이미지로 노출한 경우를 잡기 위함. data: URI·비이미지·중복은
    제외하고 ``limit`` 개로 제한(비용 보호). ``domain`` 은 현재 미사용(CDN 이미지 허용).
    """
    tree = HTMLParser(html or "")
    hinted: list[str] = []
    other: list[str] = []
    seen: set[str] = set()
    for node in tree.css("img[src]"):
        src = (node.attributes.get("src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        url = urljoin(base_url, src)
        if urlparse(url).scheme not in ("http", "https") or url in seen:
            continue
        if not url.lower().split("?", 1)[0].endswith(_IMG_EXT):
            continue
        seen.add(url)
        hint = (src + " " + (node.attributes.get("alt") or "")).lower()
        (hinted if any(h in hint for h in _MAIL_IMG_HINTS) else other).append(url)
        if len(hinted) >= limit:
            break
    return (hinted + other)[:limit]
