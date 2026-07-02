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

# 스팸봇 회피 난독화 복원: 'info (at) acme (dot) com' → 'info@acme.com'. 괄호류로 감싼
# at/dot 만 치환한다 — 맨몸 ' at ' 치환은 일반 문장 오탐 위험(정밀도 우선).
_OBFUSCATED_AT = re.compile(r"\s*[(\[{]\s*(?:at|골뱅이)\s*[)\]}]\s*", re.IGNORECASE)
_OBFUSCATED_DOT = re.compile(r"\s*[(\[{]\s*(?:dot|닷|점)\s*[)\]}]\s*", re.IGNORECASE)


def _deobfuscate(text: str) -> str:
    """난독화 표기((at)/(dot)·전각 ＠)를 표준 표기로 되돌린다."""
    text = text.replace("＠", "@")
    text = _OBFUSCATED_AT.sub("@", text)
    return _OBFUSCATED_DOT.sub(".", text)


def _decode_cfemail(payload: str) -> str | None:
    """Cloudflare email-protection 페이로드(hex)를 복호한다(첫 바이트=XOR 키)."""
    try:
        data = bytes.fromhex(payload)
        if len(data) < 2:
            return None
        return bytes(b ^ data[0] for b in data[1:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None

# 가짜 이메일 차단: ① 도메인 TLD 가 자산 확장자면 이미지/파일명 오탐(예:
# 'banner@2x-992x379.jpg' → TLD 'jpg'), ② 예시·템플릿 플레이스홀더 도메인.
_ASSET_TLDS = frozenset({
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "ico", "css", "js",
    "pdf", "zip", "mp4", "mp3", "woff", "woff2", "ttf", "eot",
})
_PLACEHOLDER_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "yourcompany.com", "company.com", "sentry.io", "sentry-cdn.com", "wix.com",
    "wixpress.com",
})


def _is_junk_email(addr: str) -> bool:
    """이미지/자산 파일명 오탐과 예시·플레이스홀더 도메인을 가짜로 판정한다."""
    domain = addr.rsplit("@", 1)[-1]
    if not domain:
        return True
    tld = domain.rsplit(".", 1)[-1]
    if tld in _ASSET_TLDS:
        return True
    # 서브도메인 포함 서픽스 매칭 — Sentry DSN(abc@o1.ingest.sentry.io) 등 SDK 키 오탐 차단.
    return any(domain == d or domain.endswith("." + d) for d in _PLACEHOLDER_DOMAINS)

# IR/문의 후보 페이지를 가리키는 링크 키워드(BFS 우선순위).
_IR_HINTS = ("investor", "ir", "투자", "투자자")
_CONTACT_HINTS = ("contact", "inquiry", "문의", "고객", "about", "회사소개", "company")

# 이미지/문서 등 따라가지 않을 확장자.
_SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".doc", ".docx", ".xls")

# OCR 대상 이미지 확장자.
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
# 이메일이 담겼을 가능성이 높은 이미지 힌트(src/alt) — 우선 OCR.
_MAIL_IMG_HINTS = ("mail", "email", "contact", "이메일", "메일", "문의")

# 문의 성격 <form> 판정 키워드(action/id/class/name/aria-label + 폼 텍스트, 다국어).
_FORM_HINTS = (
    # 한국어
    "문의", "문의하기", "온라인문의", "1:1", "상담", "상담신청", "견적", "견적문의",
    "제휴", "제휴문의", "고객센터", "고객지원", "연락", "연락처",
    # 영어
    "contact", "inquiry", "enquiry", "get in touch", "getintouch", "support",
    # 일본어
    "お問い合わせ", "問い合わせ", "お問合せ",
)
# iframe 임베드 제3자 폼 제공자 — 호스트(서브도메인 포함)로 앵커링해 매칭한다.
# (전체 URL 부분일치는 utm_source=typeform.com 같은 쿼리값 오탐을 부른다 — 호스트만 본다.)
_FORM_EMBED_HOSTS = (
    "forms.gle", "typeform.com", "jotform.com", "form.naver.com", "naver.me",
    "tally.so", "hsforms.com", "hsforms.net", "forms.office.com",
    "surveymonkey.com", "wufoo.com", "formstack.com", "zohopublic.com",
)
# 호스트만으로 못 가르는 제공자(공용 도메인 + 경로) — host+path 접두로 본다.
_FORM_EMBED_HOSTPATHS = ("docs.google.com/forms", "zoho.com/forms")
# 오탐 폼 키워드 — 검색/로그인/뉴스레터구독은 문의폼이 아니다(J컬럼 오염 방지).
_FORM_EXCLUDE_HINTS = (
    "search", "검색", "login", "log-in", "signin", "sign-in", "로그인",
    "newsletter", "subscribe", "구독", "mailing", "메일링",
)
# 문의 페이지(폼 미탐지 시 폴백) 판정용 — 경로는 세그먼트 일치(부분일치 금지: /support·
# /customer 등 광역 오탐 차단), 한국어는 토큰분해가 안 돼 고정밀 단어만 부분일치.
_CONTACT_PATH_SEGMENTS = ("contact", "contactus", "inquiry", "enquiry")
_CONTACT_PATH_SUBSTR = ("문의",)
_CONTACT_TITLE_HINTS = ("문의", "contact us", "contactus", "inquiry", "enquiry")


def extract_emails(
    html: str,
    *,
    source_url: str = "",
    method: ExtractMethod = ExtractMethod.STATIC,
    tree: HTMLParser | None = None,
) -> list[Contact]:
    """mailto 링크 + 본문 텍스트에서 이메일을 추출(HR/언론 배제).

    ``method`` 로 추출 출처를 표기한다(정적/헤드리스/OCR 등 — 신뢰도 추적용). ``tree`` 가
    주어지면 그 파싱 결과를 재사용한다(같은 페이지를 여러 추출기가 공유 — 재파싱 제거).
    """
    tree = tree if tree is not None else HTMLParser(html or "")
    found: dict[str, Contact] = {}

    def _add(addr: str, confidence: float) -> None:
        addr = addr.strip().strip(".").lower()
        if not addr or addr in found or _is_junk_email(addr):
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

    # Cloudflare 이메일 보호(XOR 인코딩) — 렌더 없이 정적 복호 가능(스팸봇 회피 표기).
    for node in tree.css("[data-cfemail]"):
        decoded = _decode_cfemail(node.attributes.get("data-cfemail") or "")
        for m in _EMAIL_RE.findall(decoded or ""):
            _add(m, 0.9)  # 사이트가 의도적으로 게시한 이메일 — mailto 급 신뢰.
    for node in tree.css("a[href*='/cdn-cgi/l/email-protection#']"):
        href = node.attributes.get("href") or ""
        decoded = _decode_cfemail(href.split("#", 1)[-1])
        for m in _EMAIL_RE.findall(decoded or ""):
            _add(m, 0.9)

    # 마크업이 아닌 추출 텍스트(난독화 복원 후), 길이 상한 적용.
    text = _deobfuscate((tree.text() or "")[:_MAX_TEXT_SCAN])
    for m in _EMAIL_RE.findall(text):
        _add(m, 0.6)

    # 원문 HTML 스캔(저신뢰) — JSON-LD("email": …)·인라인 스크립트·속성·주석 속 이메일.
    # 마크업 노이즈는 junk 필터(자산 TLD·플레이스홀더 서픽스)와 role 필터가 거른다.
    for m in _EMAIL_RE.findall((html or "")[:_MAX_TEXT_SCAN]):
        _add(m, 0.4)

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
    for m in _EMAIL_RE.findall(_deobfuscate((text or "")[:_MAX_TEXT_SCAN])):
        addr = m.strip().strip(".").lower()
        if addr in found or _is_junk_email(addr):
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


def extract_phones(
    html: str, *, source_url: str = "", tree: HTMLParser | None = None
) -> list[Contact]:
    """tel 링크 + 본문에서 전화번호를 추출(노이즈 제거). ``tree`` 주어지면 재사용."""
    tree = tree if tree is not None else HTMLParser(html or "")
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


def _embedded_form_url(tree: HTMLParser, page_url: str) -> str | None:
    """iframe src 가 알려진 제3자 폼 제공자면 그 절대 URL 을 돌려준다(JS 폼도 잡음).

    호스트(서브도메인 포함)로 앵커링해, 쿼리/경로에 제공자명이 우연히 든 광고·트래킹
    iframe 오탐을 막는다.
    """
    for ifr in tree.css("iframe[src]"):
        src = (ifr.attributes.get("src") or "").strip()
        if not src:
            continue
        parsed = urlparse(urljoin(page_url, src).lower())
        host = parsed.netloc
        hostpath = host + parsed.path
        if any(host == h or host.endswith("." + h) for h in _FORM_EMBED_HOSTS) or any(
            hostpath.startswith(hp) for hp in _FORM_EMBED_HOSTPATHS
        ):
            return urljoin(page_url, src)
    return None


def _is_excluded_form(form) -> bool:  # noqa: ANN001 (selectolax 노드 타입 비공개)
    """검색/로그인/뉴스레터구독 폼이면 True — 문의폼 오탐을 거른다."""
    if form.css_first("input[type='password']") is not None:
        return True  # 비밀번호 입력 → 로그인 폼.
    if (form.attributes.get("role") or "").lower() == "search":
        return True
    haystack = " ".join(
        (form.attributes.get(a) or "")
        for a in ("action", "id", "class", "name", "aria-label")
    ).lower()
    # 사용자 입력 가능한 필드(hidden/submit/button 등 제외) 개수 — 단일 입력 = 검색/구독 신호.
    fields = [
        n
        for n in form.css("input, textarea, select")
        if (n.attributes.get("type") or "text").lower()
        not in ("hidden", "submit", "button", "checkbox", "radio", "image", "reset")
    ]
    has_message = form.css_first("textarea") is not None
    types = {(n.attributes.get("type") or "text").lower() for n in form.css("input")}
    if "search" in types and len(fields) <= 1:
        return True  # 검색 input 단독.
    # 뉴스레터/구독·검색·로그인 키워드 + 메시지필드 없음 + 입력 1개 이하 → 문의폼 아님.
    if (
        not has_message
        and len(fields) <= 1
        and any(h in haystack for h in _FORM_EXCLUDE_HINTS)
    ):
        return True
    return False


def extract_form(
    html: str,
    *,
    page_url: str,
    method: ExtractMethod = ExtractMethod.STATIC,
    tree: HTMLParser | None = None,
) -> Contact | None:
    """문의 성격의 폼이 있으면 폼 URL 연락처를 만든다(이메일 없을 때 폴백용).

    탐지 우선순위(높을수록 신뢰): ① iframe 임베드 제3자 폼(Google Forms/Typeform/네이버폼
    등) ② textarea(메시지) 있는 진짜 문의폼 ③ 키워드만 맞는 폼. 검색/로그인/뉴스레터구독
    폼은 ``contact`` 글자가 있어도 배제한다(:func:`_is_excluded_form`). 결정적(문서순 +
    동점 시 먼저 나온 것 유지). ``tree`` 주어지면 재사용(같은 페이지 재파싱 제거).
    """
    tree = tree if tree is not None else HTMLParser(html or "")
    # ① 제3자 임베드 폼 — JS 렌더와 무관하게 src 로 잡힌다(최우선·고신뢰).
    embed = _embedded_form_url(tree, page_url)
    if embed:
        return Contact(
            type=ContactType.FORM,
            value=embed,
            extract_method=method,
            source_url=page_url,
            confidence=0.7,
        )
    # ② 페이지 내 <form> — 오탐 배제 후 메시지(textarea)폼 우선.
    best_rank = 0
    best: Contact | None = None
    for form in tree.css("form"):
        haystack = " ".join(
            (form.attributes.get(a) or "")
            for a in ("action", "id", "class", "name", "aria-label")
        ).lower()
        text = (form.text() or "").lower()
        if not any(h in haystack or h in text for h in _FORM_HINTS):
            continue
        if _is_excluded_form(form):
            continue
        has_message = form.css_first("textarea") is not None
        rank = 2 if has_message else 1
        if rank <= best_rank:
            continue  # 동점/하위는 먼저 나온 것 유지(결정적).
        action = form.attributes.get("action") or ""
        best = Contact(
            type=ContactType.FORM,
            value=urljoin(page_url, action) if action else page_url,
            extract_method=method,
            source_url=page_url,
            confidence=0.6 if has_message else 0.45,
        )
        best_rank = rank
    return best


def is_contact_page(
    url: str, html: str = "", *, tree: HTMLParser | None = None
) -> bool:
    """URL 경로 또는 title/h1 키워드로 '문의 페이지' 여부를 판정한다(폴백용 순수함수).

    정적 <form> 이 안 잡히는 JS 렌더 문의폼 페이지를 폼 미탐지 시 폴백 채택하기 위함.
    ``tree`` 가 주어지면 ``html`` 대신 그 파싱 결과로 title/h1 을 본다(재파싱 제거).
    """
    path = urlparse(url).path.lower()
    segments = set(re.split(r"[/_.\-]+", path))  # 경로 세그먼트(부분일치 광역 오탐 차단).
    if segments & set(_CONTACT_PATH_SEGMENTS) or any(s in path for s in _CONTACT_PATH_SUBSTR):
        return True
    if tree is not None or html:
        tree = tree if tree is not None else HTMLParser(html)
        title_node = tree.css_first("title")
        title = (title_node.text() if title_node else "") or ""
        h1 = " ".join((n.text() or "") for n in tree.css("h1"))
        blob = (title + " " + h1).lower()
        if any(h in blob for h in _CONTACT_TITLE_HINTS):
            return True
    return False


def candidate_links(
    html: str, *, base_url: str, domain: str | None, limit: int = 20,
    tree: HTMLParser | None = None,
) -> list[str]:
    """홈페이지에서 IR/문의 후보 내부 링크를 우선순위 순으로 추린다(동일 도메인).

    적대적 페이지의 과도한 앵커 열거를 막기 위해 ``limit`` 개 모으면 조기 종료한다.
    ``tree`` 주어지면 재사용(같은 페이지 재파싱 제거).
    """
    tree = tree if tree is not None else HTMLParser(html or "")
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
    html: str, *, base_url: str, domain: str | None = None, limit: int = 5,
    tree: HTMLParser | None = None,
) -> list[str]:
    """OCR 대상 이미지 URL 을 추리되, 이메일 가능성 높은 것(src/alt 힌트)을 우선한다.

    스팸회피로 이메일을 이미지로 노출한 경우를 잡기 위함. data: URI·비이미지·중복은
    제외하고 ``limit`` 개로 제한(비용 보호). ``domain`` 은 현재 미사용(CDN 이미지 허용).
    ``tree`` 주어지면 재사용(같은 페이지 재파싱 제거).
    """
    tree = tree if tree is not None else HTMLParser(html or "")
    hinted: list[str] = []
    other: list[str] = []
    seen: set[str] = set()
    for node in tree.css("img[src]"):
        # 적대적 페이지의 과도한 <img> 열거를 막기 위해 limit 개 모으면 조기 종료.
        if len(hinted) + len(other) >= limit:
            break
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
    return (hinted + other)[:limit]
