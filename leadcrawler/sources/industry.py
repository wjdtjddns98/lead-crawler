"""업종명 → 산업분류 코드 매핑(베스트에포트).

발견 단계에서 세그먼트의 업종명(예: "건설")을 등록처 코드로 옮긴다:
- DART: KSIC 소분류(3자리) 접두 매칭(``induty_code``).
- EDGAR: SIC(4자리) 접두 매칭(``sic``).

매핑에 없는 업종은 ``None`` 을 돌려주며, 이때 호출부는 업종 필터를 건너뛴다(전량 후보 +
상한 적용). 표는 의도적으로 부분집합이며 운영하며 확장한다.
"""

from __future__ import annotations

from .taxonomy import UNCLASSIFIED

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


# 업종명(소문자) → 검색용 영어 동의어 목록(다중 쿼리). SERP(Serper)는 쿼리당 ~10건만
# 주므로 단일어 1쿼리로는 커버리지가 빈약하다 → 동의어 여러 개로 각각 쿼리해 합집합을
# 모은다(예: 바이오 → biotech·biotechnology·pharmaceutical·life sciences). 각 항목은 IR
# 키워드("company official website …")와 결합되므로 'company' 접미 없는 업종 명사로 둔다.
_EN_INDUSTRY_TERMS: dict[str, tuple[str, ...]] = {
    "건설": ("construction", "engineering and construction", "building contractor"),
    "제조": ("manufacturing", "manufacturer", "industrial"),
    "금융": ("financial services", "bank", "investment management"),
    "it": ("software", "information technology", "technology"),
    "소프트웨어": ("software", "SaaS", "software development"),
    "바이오": (
        "biotech", "biotechnology", "pharmaceutical", "biopharmaceutical",
        "life sciences", "drug manufacturer", "medical devices", "diagnostics",
    ),
    "제약": (
        "pharmaceutical", "biopharmaceutical", "drug manufacturer",
        "generic drugs", "vaccine manufacturer",
    ),
    "유통": ("retail", "wholesale distribution"),
    "도소매": ("retail", "wholesale"),
    "운송": ("transportation", "shipping", "freight"),
    "물류": ("logistics", "supply chain"),
    "에너지": ("energy", "oil and gas", "renewable energy"),
    "부동산": ("real estate", "property development"),
    "식품": ("food", "food and beverage"),
    "화학": ("chemicals", "specialty chemicals"),
    "자동차": ("automotive", "auto parts"),
    "반도체": ("semiconductor", "semiconductors"),
    "통신": ("telecommunications", "telecom"),
}


def industry_search_term(industry: str) -> str:
    """업종명을 영어 검색어로 옮긴다(매핑 없으면 원문 그대로 — 베스트에포트)."""
    key = industry.strip().lower()
    return _EN_INDUSTRY.get(key, industry.strip())


def industry_search_terms(industry: str) -> tuple[str, ...]:
    """업종명 → 검색용 영어 동의어 목록(다중 쿼리용). 매핑 없으면 단일 영어어(없으면 원문).

    검색 발견(SearchSource)이 세그먼트당 이 목록만큼 쿼리를 던져 합집합을 모은다 — 한글
    업종어를 영어권 색인에 그대로 넣어 헛방 나던 문제를 고치고(번역), 동의어로 커버리지를
    넓힌다(SERP 쿼리당 ~10건 한계 보완).
    """
    key = industry.strip().lower()
    terms = _EN_INDUSTRY_TERMS.get(key)
    if terms:
        return terms
    return (industry_search_term(industry),)


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


# ─────────────────────────────────────────────────────────────────────────────
# 구분(엑셀 I열) 라벨 결정 — broad 판정 · 등록처 코드 역매핑(→ 닫힌 대분류 택소노미)
# ─────────────────────────────────────────────────────────────────────────────

# broad(광범위) 업종 토큰 — 구분에 그대로 두면 필터 무가치라, 등록처 코드에서 대분류를
# 복원하거나(불가하면) '미분류'로 대체하는 대상. lower() 비교(영문 'all' 대비).
_BROAD_INDUSTRY: frozenset[str] = frozenset({"", "전체", "all", "기타", UNCLASSIFIED})

# 기존 업종명(_KSIC/_SIC/_UK_SIC 키) → 닫힌 대분류 택소노미 라벨. 등록처 코드를 대분류로
# 복원할 때, 프로젝트가 이미 벤팅한 forward 접두표를 그대로 재사용하되 대분류 라벨로 옮긴다.
# **모호한 광범위 업종('제조'·'금융'·'manufacturing'·'finance')은 의도적으로 제외** — 한
# 코드가 여러 대분류(은행/증권/보험 등)에 걸쳐 단일 확정이 불가하므로 LLM 배치로 넘긴다.
_OLD_TO_TAXO: dict[str, str] = {
    "건설": "건설·엔지니어링",
    "construction": "건설·엔지니어링",
    "바이오": "제약·바이오",
    "제약": "제약·바이오",
    "pharma": "제약·바이오",
    "소프트웨어": "IT·소프트웨어",
    "software": "IT·소프트웨어",
    "it": "IT·소프트웨어",
    "유통": "유통·도소매",
    "도소매": "유통·도소매",
    "운송": "물류·운송",
    "물류": "물류·운송",
    "에너지": "에너지·전력",
    "energy": "에너지·전력",
    "부동산": "부동산·개발",
    "식품": "식품·음료",
    "화학": "화학·석유화학",
    "자동차": "자동차·모빌리티",
    "반도체": "반도체·디스플레이",
    "semiconductor": "반도체·디스플레이",
    "통신": "통신·네트워크",
}


# 역매핑에서 제외할 저정밀 접두 — 한 코드군이 여러 대분류에 걸쳐 '확신 단일매치'로 오라벨을
# 만들어 LLM 교정을 우회하는 것을 막는다: SIC 49=전기·가스·'위생'(수도·폐기물 포함),
# SIC 8731=일반 물리·생물 '연구'. 역매핑에서 빼면 해당 코드는 None→LLM 배치로 흘러가 정확히
# 분류된다. forward 필터표(_SIC 등, 구체 업종 검색 필터)는 그대로 둔다.
_REVERSE_PREFIX_DENY: frozenset[str] = frozenset({"49", "8731"})


def _invert_to_taxo(forward: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """forward 접두표(업종명→접두)를 대분류 역매핑표(대분류→접두집합)로 뒤집는다.

    ``_OLD_TO_TAXO`` 에 있는(=모호하지 않은) 업종만 채택하고, 같은 대분류로 매핑되는 여러
    업종의 접두를 합친다(예: '바이오'·'제약' 둘 다 '제약·바이오'). 저정밀 접두
    (:data:`_REVERSE_PREFIX_DENY`)는 제외해 확신-오라벨을 방지한다.
    """
    out: dict[str, set[str]] = {}
    for old, prefixes in forward.items():
        taxo = _OLD_TO_TAXO.get(old.strip().lower())
        if taxo is None:
            continue
        out.setdefault(taxo, set()).update(p for p in prefixes if p not in _REVERSE_PREFIX_DENY)
    return {label: tuple(sorted(prefixes)) for label, prefixes in out.items() if prefixes}


# 대분류 라벨 → 등록처 코드 접두집합(역매핑용). forward 표에서 파생.
_KSIC_TAXO: dict[str, tuple[str, ...]] = _invert_to_taxo(_KSIC)
_SIC_TAXO: dict[str, tuple[str, ...]] = _invert_to_taxo(_SIC)
_UK_SIC_TAXO: dict[str, tuple[str, ...]] = _invert_to_taxo(_UK_SIC)


def _reverse_lookup(code: object, table: dict[str, tuple[str, ...]]) -> str | None:
    """등록처 코드 → 대분류 라벨. **명확한 단일 최장접두 매치만** 확정한다.

    코드를 감싸는 접두 중 가장 긴 것을 고르되, 같은 최장 길이로 **둘 이상의 대분류**가
    걸리면(모호) ``None`` 을 반환한다. 미매핑도 ``None``. → 호출부(파이프라인)가 모호/미매핑을
    LLM 배치로 넘긴다("미분류·애매한 분류는 무조건 LLM"). 결정론적(입력만의 함수).
    """
    if code is None or code == "":
        return None
    code_str = str(code)
    best_len = -1
    best_labels: set[str] = set()
    for label, prefixes in table.items():
        matched = max((len(p) for p in prefixes if code_str.startswith(p)), default=-1)
        if matched < 0:
            continue
        if matched > best_len:
            best_len, best_labels = matched, {label}
        elif matched == best_len:
            best_labels.add(label)
    if best_len < 0 or len(best_labels) != 1:
        return None  # 미매핑 또는 동률(모호) → LLM 배치로 위임.
    return next(iter(best_labels))


def industry_from_ksic(code: object) -> str | None:
    """KSIC 코드(DART induty_code) → 대분류 라벨(명확 단일매치만, 없으면 None)."""
    return _reverse_lookup(code, _KSIC_TAXO)


def industry_from_sic(code: object) -> str | None:
    """SIC 코드(EDGAR sic) → 대분류 라벨(명확 단일매치만, 없으면 None)."""
    return _reverse_lookup(code, _SIC_TAXO)


def industry_from_uk_sic(code: object) -> str | None:
    """UK SIC 2007 코드(Companies House sic_codes) → 대분류 라벨(명확 단일매치만, 없으면 None)."""
    return _reverse_lookup(code, _UK_SIC_TAXO)


def is_broad_industry(industry: str | None) -> bool:
    """업종이 광범위 토큰('전체'·'기타'·빈값·'미분류' 등)인지 — 구분 복원 대상 판정.

    구체 업종(사용자가 명시적으로 고른 업종)·자유텍스트는 False → 그대로 보존(오라벨 방지).
    """
    return (industry or "").strip().lower() in _BROAD_INDUSTRY


def resolve_industry_label(segment_industry: str, *, code_label: str | None = None) -> str:
    """구분(엑셀 I열)에 기입할 라벨을 정한다.

    - **비-broad**(구체 업종 검색·자유텍스트): 그대로 보존한다(이미 그 업종으로 필터돼 있어
      코드 복원이 불필요하고, 코드 복원이 오히려 오라벨을 부를 수 있다).
    - **broad**('전체'·'기타'·빈값): 등록처 코드에서 복원한 ``code_label``(명확 단일매치),
      없으면 :data:`UNCLASSIFIED`. 후자는 파이프라인이 이후 LLM 배치를 시도한다.
    """
    if not is_broad_industry(segment_industry):
        return segment_industry
    return code_label or UNCLASSIFIED
