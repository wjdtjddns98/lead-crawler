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


def test_ir_preferred_over_general() -> None:
    best = select_best_email([_email("info@x.com", 0.9), _email("ir@x.com", 0.5)])
    assert best is not None and best.value == "ir@x.com"


def test_hr_and_press_excluded() -> None:
    best = select_best_email([_email("hr@x.com"), _email("press@x.com")])
    assert best is None


def test_general_used_when_no_ir() -> None:
    best = select_best_email([_email("contact@x.com"), _email("hr@x.com")])
    assert best is not None and best.value == "contact@x.com"
