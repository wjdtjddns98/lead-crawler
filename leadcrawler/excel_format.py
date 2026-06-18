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
    "담당자",          # H  공란
    "구분",            # I  업종만
    "이메일 실존 여부",  # J  O/X (예외: 폼만 있으면 아래 문구)
    "사이트 실존 여부",  # K  O/X
    "기타",            # L  공란
]

# 이메일이 없고 문의폼만 있을 때 J(이메일 실존 여부)에 기입하는 문구.
FORM_ONLY_NOTE = "사이트 내 문의폼"


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

    return [
        c.country,                                  # A 국가
        c.name,                                     # B 업체명
        lead.phone.value if lead.phone else "",     # C 연락처(공란 허용)
        lead.email.value if has_email else "",      # D 이메일
        ox(has_form),                               # E 홈페이지 문의(O/X)
        c.homepage or c.domain or "",               # F 사이트
        "",                                          # G 담당 부서(공란)
        "",                                          # H 담당자(공란)
        c.industry,                                 # I 구분(업종만)
        email_exist,                                # J 이메일 실존 여부
        ox(c.site_alive),                           # K 사이트 실존 여부
        "",                                          # L 기타(공란)
    ]
