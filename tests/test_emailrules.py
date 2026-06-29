"""이메일 role 분류·채택 규칙 테스트 (PO 확정 규칙)."""

from __future__ import annotations

from leadcrawler.emailrules import classify_role, select_best_email
from leadcrawler.models import Contact, ContactType, EmailRole


def _email(value: str, conf: float = 0.5) -> Contact:
    return Contact(type=ContactType.EMAIL, value=value, confidence=conf)


def test_classify_roles() -> None:
    assert classify_role("ir@x.com") is EmailRole.IR
    assert classify_role("investors@x.com") is EmailRole.IR
    assert classify_role("hr@x.com") is EmailRole.HR
    assert classify_role("press@x.com") is EmailRole.PRESS
    assert classify_role("info@x.com") is EmailRole.GENERAL


def test_short_keys_match_only_as_whole_token() -> None:
    """2글자 키(ir/hr/pr)가 사람 이름·일반 단어에 substring 으로 오분류되지 않는다."""
    # substring 매칭이면 'ir' 가 박혀 IR 로 오분류되던 개인주소 — 이제 GENERAL.
    assert classify_role("shirley@x.com") is EmailRole.GENERAL
    assert classify_role("irene@x.com") is EmailRole.GENERAL
    # 'chris' 에 'hr' substring → 과거 HR(배제)로 리드손실. 이제 GENERAL(채택).
    assert classify_role("chris@x.com") is EmailRole.GENERAL
    # 'express' 는 'press' 를 substring 으로 포함 → 과거 PRESS(배제). 이제 GENERAL.
    assert classify_role("express@x.com") is EmailRole.GENERAL


def test_keys_match_as_dotted_or_numeric_tokens() -> None:
    """구분자·숫자 경계로 분리된 토큰은 완전일치로 잡힌다."""
    assert classify_role("ir.team@x.com") is EmailRole.IR
    assert classify_role("ir2024@x.com") is EmailRole.IR
    assert classify_role("investor.relations@x.com") is EmailRole.IR
    assert classify_role("hr-team@x.com") is EmailRole.HR
    assert classify_role("pr@x.com") is EmailRole.PRESS


def test_common_concatenated_compounds() -> None:
    """흔한 결합형(구분자 없는 합성어)도 명시 토큰 키로 분류된다."""
    assert classify_role("investorrelations@x.com") is EmailRole.IR
    assert classify_role("pressroom@x.com") is EmailRole.PRESS
    assert classify_role("humanresources@x.com") is EmailRole.HR


def test_korean_keys_substring_match() -> None:
    """한글 키는 substring 으로 매칭(ascii 이름 오탐 불가)."""
    assert classify_role("채용@x.com") is EmailRole.HR
    assert classify_role("홍보팀@x.com") is EmailRole.PRESS


def test_ir_preferred_over_general() -> None:
    best = select_best_email([_email("info@x.com", 0.9), _email("ir@x.com", 0.5)])
    assert best is not None and best.value == "ir@x.com"


def test_hr_and_press_excluded() -> None:
    best = select_best_email([_email("hr@x.com"), _email("press@x.com")])
    assert best is None


def test_general_used_when_no_ir() -> None:
    best = select_best_email([_email("contact@x.com"), _email("hr@x.com")])
    assert best is not None and best.value == "contact@x.com"
