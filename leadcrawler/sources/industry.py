"""업종명 → 산업분류 코드 매핑(베스트에포트).

발견 단계에서 세그먼트의 업종명(예: "건설")을 등록처 코드로 옮긴다:
- DART: KSIC 소분류(3자리) 접두 매칭(``induty_code``).
- EDGAR: SIC(4자리) 접두 매칭(``sic``).

매핑에 없는 업종은 ``None`` 을 돌려주며, 이때 호출부는 업종 필터를 건너뛴다(전량 후보 +
상한 적용). 표는 의도적으로 부분집합이며 운영하며 확장한다.
"""

from __future__ import annotations

# 업종명(소문자) → KSIC 소분류 접두(3자리) 집합. induty_code.startswith 로 매칭.
_KSIC: dict[str, tuple[str, ...]] = {
    "건설": ("41", "42"),
    "제조": ("1", "2", "3"),
    "금융": ("64", "65", "66"),
    "it": ("58", "62", "63"),
    "소프트웨어": ("58", "62"),
    "바이오": ("21",),
    "제약": ("21",),
    "유통": ("46", "47"),
    "도소매": ("45", "46", "47"),
    "운송": ("49", "50", "51", "52"),
    "물류": ("49", "52"),
    "에너지": ("35",),
    "부동산": ("68",),
    "식품": ("10", "11"),
    "화학": ("20",),
    "자동차": ("30",),
    "반도체": ("26",),
    "통신": ("61",),
}

# 업종명(소문자) → UK SIC 2007 접두 집합. Companies House sic_codes.startswith 로 매칭.
_UK_SIC: dict[str, tuple[str, ...]] = {
    "건설": ("41", "42", "43"),
    "construction": ("41", "42", "43"),
    "제조": ("10", "11", "13", "20", "21", "25", "26", "27", "28", "29", "30", "31", "32", "33"),
    "금융": ("64", "65", "66"),
    "finance": ("64", "65", "66"),
    "it": ("62", "63"),
    "소프트웨어": ("62",),
    "software": ("62",),
    "바이오": ("21", "72"),
    "제약": ("21",),
    "pharma": ("21",),
    "유통": ("46", "47"),
    "에너지": ("35",),
    "energy": ("35",),
    "부동산": ("68",),
    "반도체": ("26",),
    "통신": ("61",),
}

# 업종명(소문자) → SIC 접두 집합. sic.startswith 로 매칭.
_SIC: dict[str, tuple[str, ...]] = {
    "건설": ("15", "16", "17"),
    "construction": ("15", "16", "17"),
    "제조": ("2", "3"),
    "금융": ("60", "61", "62"),
    "finance": ("60", "61", "62"),
    "it": ("737",),
    "소프트웨어": ("7372",),
    "software": ("7372",),
    "바이오": ("283", "8731"),
    "제약": ("283",),
    "pharma": ("283",),
    "유통": ("52", "53", "54", "59"),
    "에너지": ("13", "29", "49"),
    "energy": ("13", "29"),
    "반도체": ("3674",),
    "semiconductor": ("3674",),
    "통신": ("48",),
}


def ksic_prefixes(industry: str) -> tuple[str, ...] | None:
    """업종명에 대응하는 KSIC 접두 집합(없으면 None)."""
    return _KSIC.get(industry.strip().lower())


def sic_prefixes(industry: str) -> tuple[str, ...] | None:
    """업종명에 대응하는 SIC 접두 집합(없으면 None)."""
    return _SIC.get(industry.strip().lower())


def uk_sic_prefixes(industry: str) -> tuple[str, ...] | None:
    """업종명에 대응하는 UK SIC 2007 접두 집합(없으면 None)."""
    return _UK_SIC.get(industry.strip().lower())


def matches_prefix(code: object, prefixes: tuple[str, ...] | None) -> bool:
    """코드가 접두 집합 중 하나로 시작하면 True. prefixes 가 None 이면 필터 통과(전량).

    ``code`` 는 외부 JSON 에서 오므로 숫자 등 비문자열일 수 있어 방어적으로 문자열화한다.
    """
    if prefixes is None:
        return True
    if code is None or code == "":
        return False
    code_str = str(code)
    return any(code_str.startswith(p) for p in prefixes)
