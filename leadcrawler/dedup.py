"""중복 제거 — canonical_key 산정.

제약 ①(이미 검색한 기업 재추출 금지)의 핵심. 우선순위:
``registry_id`` → 정규화 도메인(eTLD+1 근사) → 정규화 회사명+국가.
같은 기업이 여러 소스에서 잡혀도 동일 key 로 모이도록 한다.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# 회사명에서 떼어낼 흔한 법인격 접미사(정규화용).
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "llc", "llp", "lp", "plc", "gmbh", "ag", "sa", "srl", "bv", "nv", "oy", "ab",
    "pte", "pty", "주식회사", "유한회사", "재단법인", "사단법인",
}
_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^0-9a-z가-힣]+")


def normalize_name(name: str) -> str:
    """회사명을 비교용으로 정규화한다(소문자·법인격 제거·기호 제거)."""
    s = (name or "").strip().lower()
    s = _NON_ALNUM.sub(" ", s)
    tokens = [t for t in _WS.sub(" ", s).split() if t and t not in _LEGAL_SUFFIXES]
    return "".join(tokens)


def normalize_domain(value: str | None) -> str | None:
    """URL/도메인 문자열에서 등록 도메인(eTLD+1 근사)을 추출한다.

    완전한 공개 접미사 목록(PSL) 대신 단순 휴리스틱을 쓴다 — 2단계 국가코드
    (co.kr, co.jp, com.cn 등)는 3레이블을 유지한다.
    """
    if not value:
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    if "//" not in raw:
        raw = "//" + raw
    host = urlparse(raw).hostname or ""
    host = host.removeprefix("www.")
    if not host or "." not in host:
        return None
    labels = host.split(".")
    two_level_tlds = {"co", "com", "or", "ne", "go", "ac", "gov", "edu", "org"}
    if len(labels) >= 3 and labels[-2] in two_level_tlds and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def canonical_key(
    *,
    registry: str | None = None,
    registry_id: str | None = None,
    domain: str | None = None,
    name: str | None = None,
    country: str | None = None,
) -> str:
    """기업 식별용 canonical_key 를 우선순위에 따라 생성한다."""
    if registry and registry_id:
        return f"reg:{registry.strip().lower()}:{registry_id.strip().lower()}"
    norm_domain = normalize_domain(domain)
    if norm_domain:
        return f"dom:{norm_domain}"
    norm_name = normalize_name(name or "")
    if norm_name:
        return f"name:{(country or '').strip().lower()}:{norm_name}"
    raise ValueError("canonical_key 를 만들 식별 정보가 없습니다(registry/domain/name 모두 없음)")
