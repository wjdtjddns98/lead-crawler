"""크롤 업종 어휘의 택소노미 통일 — 라벨 조회 확장 + supported_industries 택소노미 노출."""

from __future__ import annotations

from leadcrawler.sources.industry import (
    industry_search_terms,
    is_specific_industry,
    ksic_prefixes,
    sic_prefixes,
    supported_industries,
    uk_sic_prefixes,
)
from leadcrawler.sources.taxonomy import INDUSTRY_TAXONOMY


def test_taxonomy_label_resolves_code_prefixes() -> None:
    # 택소노미 라벨이 기존 키(바이오∪제약 등)의 코드 접두 합집합으로 풀린다.
    assert ksic_prefixes("제약·바이오") == ("21",)
    assert sic_prefixes("제약·바이오") == ("283", "8731")
    assert uk_sic_prefixes("건설·엔지니어링") == ("41", "42", "43")
    assert ksic_prefixes("IT·소프트웨어") == ("58", "62", "63")


def test_taxonomy_label_union_search_terms() -> None:
    terms = industry_search_terms("제약·바이오")
    # 바이오·제약 양쪽 고유 동의어가 모두 포함되고 중복은 없어야 한다.
    assert "biotech" in terms
    assert "generic drugs" in terms
    assert len(terms) == len(set(terms))


def test_taxonomy_label_is_specific() -> None:
    # 택소노미 라벨은 코드 매핑 유무와 무관하게 전부 구체(집계원 OFF) — 매핑 없는
    # 라벨에 집계원을 켜두면 업종 무관 명부가 라벨 도장을 받는 오라벨 사고(gleif 실측).
    assert is_specific_industry("IT·소프트웨어")
    assert is_specific_industry("게임")
    assert is_specific_industry("기계·산업장비")
    # 전 라벨 회귀가드 — 미래 택소노미 추가분이 매핑 기반 구로직으로 새는 것 방지.
    # casefold 입력도 통과해야 한다(입력 경계 함수 — "ai·데이터" 소문자 케이스 HIGH).
    for lbl in INDUSTRY_TAXONOMY:
        assert is_specific_industry(lbl), lbl
        assert is_specific_industry(lbl.casefold()), lbl
    # '기타 제조'는 AMBIGUOUS(강제 재분류 대상)지만 is_broad_industry 도 False 라
    # 구체 취급이 두 게이트 함수의 정합 — 광범위로 되돌리면 기존 비일관 재도입.
    assert is_specific_industry("기타 제조")
    # 광범위 토큰은 여전히 비구체(집계원 유지 — 광범위 발견 경로 보존).
    assert not is_specific_industry("전체")
    assert not is_specific_industry("")


def test_old_keys_still_work() -> None:
    # 저장된 크롤 타깃("바이오,제약" 등) 하위호환 — 기존 키 조회 불변.
    assert ksic_prefixes("바이오") == ("21",)
    assert industry_search_terms("제약")[0] == "pharmaceutical"
    assert is_specific_industry("반도체")


def test_expanded_labels_resolve_registry_prefixes() -> None:
    # SIC 확장(자동차·모빌리티): 등록처 가드가 더는 스킵 안 하도록 접두가 풀려야 한다.
    assert uk_sic_prefixes("자동차·모빌리티") == ("29",)
    assert sic_prefixes("자동차·모빌리티") == ("371",)
    # 식품·음료.
    assert uk_sic_prefixes("식품·음료") == ("10", "11")
    assert sic_prefixes("식품·음료") == ("20",)
    # 화학·석유화학: 283(제약) 제외 큐레이션 — 제약 과포함 없이 화학만.
    assert sic_prefixes("화학·석유화학") == ("281", "282", "284", "285", "286", "287", "289")
    assert "283" not in sic_prefixes("화학·석유화학")
    assert uk_sic_prefixes("화학·석유화학") == ("20",)
    # 물류·운송: 운송∪물류 합집합(라벨은 둘 다 물류·운송로 귀속).
    assert sic_prefixes("물류·운송") == ("40", "42", "44", "45", "46", "47")
    assert uk_sic_prefixes("물류·운송") == ("49", "50", "51", "52")
    # 부동산·개발(US SIC 65).
    assert sic_prefixes("부동산·개발") == ("65",)
    # 확장 라벨은 모두 구체 업종으로 판정 → 집계원 게이팅 대상.
    for lbl in ("자동차·모빌리티", "식품·음료", "화학·석유화학", "물류·운송", "부동산·개발"):
        assert is_specific_industry(lbl)


def test_supported_industries_are_taxonomy_labels() -> None:
    labels = [ko for ko, _ in supported_industries()]
    assert labels
    assert set(labels) <= set(INDUSTRY_TAXONOMY)
    assert "제약·바이오" in labels
    # 모호 키는 단일 대분류 확정 불가로 미노출(비택소노미 구분 라벨 파편화 방지).
    assert "제조" not in labels
    assert "금융" not in labels
    # 영문 검색어도 같이 내려간다(UI 별칭 검색용).
    en = dict(supported_industries())["제약·바이오"]
    assert en == "biotech"
