"""이메일 role 분류 및 채택 규칙 (PO 확정).

규칙:
- IR 이메일 최우선(``ir@``, ``investor(s)@``, 투자 컨텍스트).
- 없으면 ``contact@``·``info@`` 등 공통 메일박스 허용.
- **HR/채용·언론/홍보 성격은 배제**(발송 대상에서 제외).
"""

from __future__ import annotations

import re

from .models import ACCEPTED_EMAIL_ROLES, Contact, ContactType, EmailRole

# local-part 키워드 → role. 가장 구체적인 것부터 검사한다.
# 짧은 ascii 키('ir'·'hr'·'pr')는 **토큰 완전일치**로만 매칭한다(아래 _matches 참조) —
# substring 매칭은 'shirley'·'irene'→IR, 'chris'→HR, 'express'→PRESS 처럼 사람 이름/일반
# 단어를 오분류한다('express' 는 'press' 를 substring 으로 포함). 그래서 흔한 결합형
# ('investorrelations'·'pressroom' 등)은 별도 토큰 키로 명시 등록한다.
_IR_KEYS = ("ir", "investor", "investors", "investorrelations", "투자")
_HR_KEYS = (
    "hr", "recruit", "recruiting", "recruitment", "career", "careers", "jobs",
    "humanresources", "인사", "채용",
)
_PRESS_KEYS = (
    "press", "media", "pr", "news", "pressroom", "newsroom", "mediarelations",
    "publicrelations", "홍보", "보도",
)
_GENERAL_KEYS = (
    "contact", "contactus", "info", "inquiry", "enquiry", "hello", "help",
    "support", "sales", "office", "admin", "mail", "master", "webmaster", "문의",
)

# local-part 를 토큰으로 쪼갠다: 구분자(. _ -)와 영문↔숫자 경계에서 분리.
# 예: 'investor.relations'→['investor','relations'], 'ir2024'→['ir','2024'].
_TOKEN_SPLIT = re.compile(r"[._\-]+|(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")


def _tokens(local: str) -> set[str]:
    """local-part 의 토큰 집합(빈 토큰 제거)."""
    return {t for t in _TOKEN_SPLIT.split(local) if t}


def _matches(keys: tuple[str, ...], tokens: set[str], local: str) -> bool:
    """키워드 매칭 — ascii 키는 토큰 완전일치(오탐 차단), 비ascii(한글)는 substring.

    한글 키는 ascii 이름에 substring 으로 끼어들 수 없어 오탐이 불가능하므로 substring 으로
    유연하게 잡는다(한글 local-part 는 토큰 경계가 모호). ascii 키는 'ir'·'pr' 같은 2글자
    키가 사람 이름에 박혀 오분류되지 않도록 토큰 단위 완전일치만 허용한다.
    """
    for k in keys:
        if k.isascii():
            if k in tokens:
                return True
        elif k in local:
            return True
    return False


def classify_role(email: str) -> EmailRole:
    """이메일 주소의 local-part 로 성격(role)을 분류한다."""
    local = (email or "").split("@", 1)[0].lower()
    tokens = _tokens(local)
    if _matches(_IR_KEYS, tokens, local):
        return EmailRole.IR
    if _matches(_HR_KEYS, tokens, local):
        return EmailRole.HR
    if _matches(_PRESS_KEYS, tokens, local):
        return EmailRole.PRESS
    if _matches(_GENERAL_KEYS, tokens, local):
        return EmailRole.GENERAL
    # 그 외(개인명 등)는 일반 연락처로 간주해 허용하되 우선순위는 낮다.
    return EmailRole.GENERAL


def is_accepted(role: EmailRole) -> bool:
    """발송 대상으로 채택 가능한 role 인지(HR/언론/개인 배제)."""
    return role in ACCEPTED_EMAIL_ROLES


def accepted_emails(contacts: list[Contact]) -> list[Contact]:
    """채택 규칙에 맞는 이메일 후보 전부를 **우선순위 내림차순**으로 반환한다.

    IR > GENERAL 순으로 우선하고, 동급이면 confidence 가 높은 것, 그래도 동급이면 주소
    문자열 오름차순으로 **결정적** 정렬한다(목록·선택 UI 가 항상 같은 순서를 보이도록).
    HR/언론 등 배제 role 은 제거한다. 분류된 role 을 반영한 사본으로 반환한다.
    """
    candidates: list[Contact] = []
    for c in contacts:
        if c.type is not ContactType.EMAIL:
            continue
        role = c.role if c.role is not EmailRole.UNKNOWN else classify_role(c.value)
        if not is_accepted(role):
            continue
        candidates.append(c.model_copy(update={"role": role}))

    # 결정적 정렬: 안정정렬 2단(주소 오름차순 → role·confidence 내림차순). 동급이면
    # 주소 오름차순이 유지된다(Python sort 안정성).
    candidates.sort(key=lambda c: c.value)
    candidates.sort(key=lambda c: (1 if c.role is EmailRole.IR else 0, c.confidence), reverse=True)
    return candidates


def select_best_email(contacts: list[Contact]) -> Contact | None:
    """이메일 후보 중 채택 규칙에 맞는 최선의 1건을 고른다(:func:`accepted_emails` 의 선두)."""
    ranked = accepted_emails(contacts)
    return ranked[0] if ranked else None
