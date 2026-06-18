"""이메일 role 분류 및 채택 규칙 (PO 확정).

규칙:
- IR 이메일 최우선(``ir@``, ``investor(s)@``, 투자 컨텍스트).
- 없으면 ``contact@``·``info@`` 등 공통 메일박스 허용.
- **HR/채용·언론/홍보 성격은 배제**(발송 대상에서 제외).
"""

from __future__ import annotations

from .models import ACCEPTED_EMAIL_ROLES, Contact, ContactType, EmailRole

# local-part 키워드 → role. 가장 구체적인 것부터 검사한다.
_IR_KEYS = ("ir", "investor", "investors", "투자")
_HR_KEYS = ("hr", "recruit", "recruiting", "career", "careers", "jobs", "인사", "채용")
_PRESS_KEYS = ("press", "media", "pr", "news", "홍보", "보도")
_GENERAL_KEYS = (
    "contact", "info", "inquiry", "enquiry", "hello", "help", "support",
    "sales", "office", "admin", "mail", "master", "webmaster", "문의",
)


def classify_role(email: str) -> EmailRole:
    """이메일 주소의 local-part 로 성격(role)을 분류한다."""
    local = (email or "").split("@", 1)[0].lower()
    if any(k in local for k in _IR_KEYS):
        return EmailRole.IR
    if any(k in local for k in _HR_KEYS):
        return EmailRole.HR
    if any(k in local for k in _PRESS_KEYS):
        return EmailRole.PRESS
    if any(k in local for k in _GENERAL_KEYS):
        return EmailRole.GENERAL
    # 그 외(개인명 등)는 일반 연락처로 간주해 허용하되 우선순위는 낮다.
    return EmailRole.GENERAL


def is_accepted(role: EmailRole) -> bool:
    """발송 대상으로 채택 가능한 role 인지(HR/언론/개인 배제)."""
    return role in ACCEPTED_EMAIL_ROLES


def select_best_email(contacts: list[Contact]) -> Contact | None:
    """이메일 후보 중 채택 규칙에 맞는 최선의 1건을 고른다.

    IR > GENERAL 순으로 우선하고, 동급이면 confidence 가 높은 것을 택한다.
    HR/언론 등 배제 role 은 후보에서 제거한다.
    """
    candidates: list[Contact] = []
    for c in contacts:
        if c.type is not ContactType.EMAIL:
            continue
        role = c.role if c.role is not EmailRole.UNKNOWN else classify_role(c.value)
        if not is_accepted(role):
            continue
        # 분류된 role 을 반영한 사본으로 다룬다.
        candidates.append(c.model_copy(update={"role": role}))

    if not candidates:
        return None

    def rank(c: Contact) -> tuple[int, float]:
        role_rank = 1 if c.role is EmailRole.IR else 0
        return (role_rank, c.confidence)

    return max(candidates, key=rank)
