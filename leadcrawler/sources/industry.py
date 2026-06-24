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


# 업종명(소문자) → 영어 검색어. 라틴/영어 중심 글로벌 색인(OpenCorporates 등)에
# 한글 업종을 그대로 넣으면 거의 매칭되지 않으므로 영어 키워드로 옮긴다.
_EN_INDUSTRY: dict[str, str] = {
    "건설": "construction",
    "제조": "manufacturing",
    "금융": "finance",
    "it": "it",
    "소프트웨어": "software",
    "바이오": "biotech",
    "제약": "pharmaceutical",
    "유통": "retail",
    "도소매": "retail",
    "운송": "transport",
    "물류": "logistics",
    "에너지": "energy",
    "부동산": "real estate",
    "식품": "food",
    "화학": "chemical",
    "자동차": "automotive",
    "반도체": "semiconductor",
    "통신": "telecommunications",
}


def industry_search_term(industry: str) -> str:
    """업종명을 영어 검색어로 옮긴다(매핑 없으면 원문 그대로 — 베스트에포트)."""
    key = industry.strip().lower()
    return _EN_INDUSTRY.get(key, industry.strip())


def supported_industries() -> tuple[tuple[str, str], ...]:
    """선택 가능한 표준 업종 목록 (한글명, 영문 검색어) — 업종 선택 UI 의 단일 출처.

    여기 있는 업종만 등록처 코드(KSIC/SIC/UK SIC)로 정확히 필터된다. 자유 텍스트 입력은
    오타·미매핑으로 필터가 통째로 풀려 비대상 업종이 섞이므로(집계원 폴백), UI 는 이
    목록에서만 고르게 해 업종 정밀도를 보장한다.
    """
    return tuple(_EN_INDUSTRY.items())


def is_specific_industry(industry: str) -> bool:
    """업종이 '구체적'(알려진 매핑 업종)인지 — 집계원(GLEIF/Wikidata/거래소) 게이팅 기준.

    구체 업종이면 업종 필터를 못 하는 집계원·거래소 소스를 끄고 등록처(코드 필터)와
    검색(키워드 필터)에 맡긴다(정밀도 우선). 빈값·미매핑('전체'·'기타' 등)은 '광범위'로
    보아 집계원을 그대로 둔다(광범위 발견의 유일 출처일 수 있으므로).
    """
    key = (industry or "").strip().lower()
    if not key:
        return False
    return key in _EN_INDUSTRY or key in _KSIC or key in _SIC or key in _UK_SIC


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
