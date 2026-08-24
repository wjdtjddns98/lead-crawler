"""고정 엑셀 산출 서식 (PO 확정 12컬럼).

서식 원본: ``바탕화면\\해외 기업 리스트(견본).xlsx``.
열 순서와 O/X 규칙을 한 곳에서 정의해 export/import 가 공유한다.
"""

from __future__ import annotations

from .models import CompanyLead, ValidationStatus

# A~L 12개 헤더(원본 서식과 정확히 일치해야 함).
HEADERS: list[str] = [
    "국가",            # A
    "업체명",          # B
    "연락처",          # C  전화, 공란 허용
    "이메일",          # D
    "홈페이지 문의",    # E  O/X (문의폼 존재·검증)
    "사이트",          # F
    "담당 부서",        # G  공란
    "담당자",          # H  검수자 기입(확정 시 manager)
    "구분",            # I  업종만
    "이메일 실존 여부",  # J  O/X (예외: 폼만 있으면 아래 문구)
    "사이트 실존 여부",  # K  O/X
    "기타",            # L  공란
]

# 이메일이 없고 문의폼만 있을 때 J(이메일 실존 여부)에 기입하는 문구.
FORM_ONLY_NOTE = "사이트 내 문의폼"

# 수식/CSV 인젝션 방어: 크롤·LLM 유래 자유텍스트(업체명 등)가 수식 문자로 시작하면
# 엑셀이 =HYPERLINK/WEBSERVICE/DDE 로 실행한다. 앞에 작은따옴표를 붙여 텍스트로 못박는다.
# 전화(C)·URL(E/F)은 정당하게 +/http 로 시작하므로 대상에서 제외(오탐 방지).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def defuse(text: str) -> str:
    """수식 문자로 시작하는 텍스트 앞에 ``'`` 를 붙여 엑셀 수식 실행을 차단한다."""
    return "'" + text if text[:1] in _FORMULA_PREFIXES else text


def ox(flag: bool) -> str:
    """불리언을 대문자 O/X 로 변환한다."""
    return "O" if flag else "X"


def build_row(lead: CompanyLead) -> list[str]:
    """:class:`CompanyLead` 한 건을 12컬럼 행(문자열 리스트)으로 변환한다."""
    c = lead.company
    has_email = lead.email is not None
    has_form = lead.form is not None
    email_valid = lead.email_validation.status is ValidationStatus.VALID

    # J: 이메일 실존 여부 — 이메일 없고 폼만 있으면 안내 문구.
    if has_email:
        email_exist = ox(email_valid)
    elif has_form:
        email_exist = FORM_ONLY_NOTE
    else:
        email_exist = "X"

    # E: 홈페이지 문의 — 폼 있으면 폼 URL(클릭 이동), 없으면 X.
    form_cell = lead.form.value if has_form else "X"

    return [
        defuse(c.country),                          # A 국가
        defuse(c.name),                             # B 업체명(크롤·LLM 유래 — 수식 인젝션 방어)
        lead.phone.value if lead.phone else "",     # C 연락처(공란 허용)
        lead.email.value if has_email else "",      # D 이메일
        form_cell,                                  # E 홈페이지 문의(폼 URL 또는 X)
        c.homepage or c.domain or "",               # F 사이트
        "",                                          # G 담당 부서(공란)
        defuse(lead.manager),                       # H 담당자(검수자 기입 — 수식 인젝션 방어)
        defuse(c.industry),                         # I 구분(업종만)
        email_exist,                                # J 이메일 실존 여부
        ox(c.site_alive),                           # K 사이트 실존 여부
        defuse(lead.note),                          # L 기타(검수자 메모 — 수식 인젝션 방어)
    ]
