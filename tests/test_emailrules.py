"""이메일 role 분류·채택 규칙 테스트 (PO 확정 규칙)."""

from __future__ import annotations

from leadcrawler.emailrules import (
    MAX_EMAILS,
    accepted_emails,
    cap_emails,
    classify_role,
    select_best_email,
)
from leadcrawler.models import Contact, ContactType, EmailRole, ValidationStatus


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


def _statuses(**kv: ValidationStatus) -> dict[str, ValidationStatus]:
    """`{'ir': VALID}` → `{'ir@x.com': VALID}` (테스트 가독성용 단축)."""
    return {f"{k}@x.com": v for k, v in kv.items()}


def test_cap_emails_priority_ir_valid_then_valid_then_risky() -> None:
    """우선순위: IR 정상 > 그 외 정상 > 주의 (PO 확정 2026-07-29)."""
    cands = accepted_emails(
        [_email("info@x.com", 0.9), _email("ir@x.com", 0.5), _email("sales@x.com", 0.8)]
    )
    ranked = cap_emails(
        cands,
        _statuses(
            info=ValidationStatus.RISKY,
            ir=ValidationStatus.VALID,
            sales=ValidationStatus.VALID,
        ),
    )
    assert [c.value for c in ranked] == ["ir@x.com", "sales@x.com", "info@x.com"]


def test_cap_emails_risky_ir_ranks_below_valid_general() -> None:
    """IR 이라도 '주의'면 정상 일반메일보다 뒤 — 등급이 role 보다 상위 축이다."""
    cands = accepted_emails([_email("ir@x.com", 0.9), _email("info@x.com", 0.1)])
    ranked = cap_emails(
        cands, _statuses(ir=ValidationStatus.RISKY, info=ValidationStatus.VALID)
    )
    assert [c.value for c in ranked] == ["info@x.com", "ir@x.com"]


def test_cap_emails_drops_invalid_and_unrated() -> None:
    cands = accepted_emails(
        [_email("ir@x.com"), _email("info@x.com"), _email("sales@x.com")]
    )
    # sales 는 statuses 에 없음(등급 미상) → 제외.
    ranked = cap_emails(
        cands, _statuses(ir=ValidationStatus.INVALID, info=ValidationStatus.VALID)
    )
    assert [c.value for c in ranked] == ["info@x.com"]


def test_cap_emails_limits_to_three() -> None:
    """상한 3 — 실측 최대 92개까지 딸려오던 걸 자른다."""
    cands = accepted_emails([_email(f"info{i}@x.com") for i in range(10)])
    ranked = cap_emails(cands, {c.value: ValidationStatus.VALID for c in cands})
    assert len(ranked) == MAX_EMAILS == 3
    # 동급이면 accepted_emails 의 결정적 순서(주소 오름차순)가 보존된다.
    assert [c.value for c in ranked] == ["info0@x.com", "info1@x.com", "info2@x.com"]
