"""중복 제거 — canonical_key 산정.

제약 ①(이미 검색한 기업 재추출 금지)의 핵심. 우선순위:
``registry_id`` → 정규화 도메인(eTLD+1 근사) → 정규화 회사명+국가.
같은 기업이 여러 소스에서 잡혀도 동일 key 로 모이도록 한다.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

# canonical_key 는 DB PK(varchar(255)) 라 길이를 넘기면 PG 가 거부한다.
# 초과 시 결정적 해시로 축약(충돌 회피)하기 위한 한계값.
_KEY_MAXLEN = 255

# 회사명에서 떼어낼 흔한 법인격 접미사(정규화용).
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "llc", "llp", "lp", "plc", "gmbh", "ag", "sa", "srl", "bv", "nv", "oy", "ab",
    "pte", "pty", "주식회사", "유한회사", "재단법인", "사단법인",
}
_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^0-9a-z가-힣]+")


def tokenize_name(name: str) -> list[str]:
    """회사명을 비교용 토큰 목록으로 정규화한다(소문자·기호제거·법인격 제거).

    ``normalize_name``(concat 키)과 중복해소의 토큰셋 유사도가 같은 규칙을 쓰도록
    토큰화를 단일 출처로 둔다.
    """
    s = (name or "").strip().lower()
    s = _NON_ALNUM.sub(" ", s)
    return [t for t in _WS.sub(" ", s).split() if t and t not in _LEGAL_SUFFIXES]


def normalize_name(name: str) -> str:
    """회사명을 비교용으로 정규화한다(소문자·법인격 제거·기호 제거)."""
    return "".join(tokenize_name(name))


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


def normalize_reg_no(value: str | None) -> str | None:
    """현지 등록번호(사업자번호 등)를 비교용으로 정규화한다(영숫자만·소문자).

    "124-81-00998" 과 "1248100998" 이 같은 번호로 비교되도록 구분기호를 제거한다.
    선행 0 보존을 위해 문자열 비교만 한다(숫자 변환 금지).
    """
    if not value:
        return None
    normalized = _NON_ALNUM.sub("", value.strip().lower())
    return normalized or None


def _clamp_key(key: str) -> str:
    """255자 초과 키를 결정적으로 축약한다(접두사 보존 + 해시로 충돌 회피)."""
    if len(key) <= _KEY_MAXLEN:
        return key
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()  # 40자
    # 접두사(name: 등)와 가독 부분 일부를 남기고 끝에 해시를 붙인다.
    head = key[: _KEY_MAXLEN - len(digest) - 1]
    return f"{head}:{digest}"[:_KEY_MAXLEN]


def canonical_key(
    *,
    registry: str | None = None,
    registry_id: str | None = None,
    domain: str | None = None,
    name: str | None = None,
    country: str | None = None,
) -> str:
    """기업 식별용 canonical_key 를 우선순위에 따라 생성한다(최대 255자)."""
    if registry and registry_id:
        return _clamp_key(f"reg:{registry.strip().lower()}:{registry_id.strip().lower()}")
    norm_domain = normalize_domain(domain)
    if norm_domain:
        return _clamp_key(f"dom:{norm_domain}")
    norm_name = normalize_name(name or "")
    if norm_name:
        return _clamp_key(f"name:{normalize_country(country)}:{norm_name}")
    raise ValueError("canonical_key 를 만들 식별 정보가 없습니다(registry/domain/name 모두 없음)")


def normalize_country(country: str | None) -> str:
    """국가 표기를 ISO2 소문자로 정규화한다(``name:`` 티어 key 일관성).

    같은 도메인 없는 기업이 import 시드('대한민국')와 라이브 크롤(세그먼트 'KR')에서
    서로 다른 표기로 들어와도 동일 ``name:`` key 로 모이도록(제약 ① — 재추출 금지),
    ISO2/ISO3/영문/한글 별칭을 :func:`sources.countries.resolve_country` 단일 출처로
    해석한다. 미등록 국가는 기존 동작(원문 strip/lower)으로 폴백해 회귀를 막는다.

    ``sources`` 패키지 → ``dedup`` 역방향 import 사이클을 피하려고 지역 import 한다
    (모듈은 첫 호출 후 캐시되므로 비용 무시 가능).
    """
    from .sources.countries import resolve_country

    raw = (country or "").strip().lower()
    resolved = resolve_country(raw)
    return resolved.iso2.lower() if resolved else raw
