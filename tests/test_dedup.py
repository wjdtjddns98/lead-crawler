"""dedup canonical_key / 정규화 테스트."""

from __future__ import annotations

from leadcrawler.dedup import (
    canonical_key,
    normalize_country,
    normalize_domain,
    normalize_name,
)


def test_registry_id_takes_priority() -> None:
    key = canonical_key(registry="edgar", registry_id="0000320193", domain="apple.com")
    assert key == "reg:edgar:0000320193"


def test_domain_used_when_no_registry() -> None:
    assert canonical_key(domain="https://www.Apple.com/ir") == "dom:apple.com"


def test_name_country_fallback() -> None:
    assert canonical_key(name="삼성전자 주식회사", country="KR") == "name:kr:삼성전자"


def test_name_tier_country_normalized_to_iso2() -> None:
    # AC1: 도메인 없는 같은 기업이 표기차(한글·ISO2·영문)로 들어와도 동일 name: key.
    expected = "name:kr:삼성물산"
    assert canonical_key(name="삼성물산", country="대한민국") == expected
    assert canonical_key(name="삼성물산", country="KR") == expected
    assert canonical_key(name="삼성물산", country="korea") == expected
    assert canonical_key(name="삼성물산", country="South Korea") == expected


def test_name_tier_unregistered_country_falls_back() -> None:
    # AC2: 미등록 국가는 기존 동작(원문 strip/lower) 유지 — 회귀 0.
    assert canonical_key(name="Acme", country="Narnia") == "name:narnia:acme"
    assert canonical_key(name="Acme", country="") == "name::acme"
    assert canonical_key(name="Acme", country=None) == "name::acme"


def test_normalize_country_helper() -> None:
    assert normalize_country("대한민국") == "kr"
    assert normalize_country("미국") == "us"
    assert normalize_country("  Japan ") == "jp"
    assert normalize_country("Narnia") == "narnia"  # 미등록=폴백
    assert normalize_country(None) == ""


def test_country_normalization_only_affects_name_tier() -> None:
    # AC3: domain/registry 티어는 국가 정규화 무관(불변).
    assert canonical_key(domain="samsung.com", country="대한민국") == "dom:samsung.com"
    assert (
        canonical_key(registry="dart", registry_id="123", country="대한민국")
        == "reg:dart:123"
    )


def test_normalize_domain_handles_two_level_tld() -> None:
    assert normalize_domain("https://www.company.co.kr/contact") == "company.co.kr"
    assert normalize_domain("sub.example.com") == "example.com"


def test_normalize_name_strips_legal_suffix() -> None:
    assert normalize_name("Acme, Inc.") == normalize_name("ACME")


def test_canonical_key_is_bounded_to_255() -> None:
    # 아주 긴 이름도 PK(varchar(255)) 한계 안으로 결정적 축약(PG 오류 방지).
    long_name = "가나다라마바사" * 80  # 560자
    key = canonical_key(name=long_name, country="KR")
    assert len(key) <= 255
    # 결정적: 같은 입력은 같은 키.
    assert key == canonical_key(name=long_name, country="KR")
    # 다른 긴 이름은 다른 키(해시로 충돌 회피).
    assert key != canonical_key(name=long_name + "x", country="KR")


def test_missing_identity_raises() -> None:
    try:
        canonical_key()
    except ValueError:
        return
    raise AssertionError("식별 정보가 없으면 ValueError 여야 한다")


def test_normalize_name_folds_ascii_paren_legal_forms() -> None:
    """괄호형 KR 법인격 표기 변형이 같은 키로 접힌다(2026-07-10 리뷰 H1 — 제약① 누수)."""
    base = normalize_name("주식회사 대한화학")
    assert normalize_name("(주)대한화학") == base
    assert normalize_name("대한화학(주)") == base
    assert normalize_name("㈜대한화학") == base
    assert normalize_name("（주）대한화학") == base  # 전각 괄호.
    assert normalize_name("(유)대한화학") == normalize_name("유한회사 대한화학")
    # 오폴딩 방지 — 이름 본문 속 '주'는 보존.
    assert normalize_name("주연테크") != normalize_name("연테크")


def test_normalize_domain_preserves_multi_tenant_subdomain() -> None:
    """멀티테넌트 호스팅은 테넌트 라벨 보존 — 서로 다른 회사의 false merge 방지(전수리뷰)."""
    from leadcrawler.dedup import normalize_domain

    assert normalize_domain("https://alpha.github.io/about") == "alpha.github.io"
    assert normalize_domain("beta.github.io") == "beta.github.io"
    assert normalize_domain("https://shop.myshopify.com") == "shop.myshopify.com"
    assert normalize_domain("www.acme.modoo.at") == "acme.modoo.at"
    # 접미 자체만 오면 그대로(라벨 부족 시 인덱스 안전).
    assert normalize_domain("github.io") == "github.io"
    # 깊은 서브도메인도 테넌트 1라벨까지만.
    assert normalize_domain("a.b.pages.dev") == "b.pages.dev"
    # 기존 동작 회귀 없음: 일반 도메인·2단계 ccTLD.
    assert normalize_domain("https://www.acme.co.kr/ir") == "acme.co.kr"
    assert normalize_domain("sub.acme.com") == "acme.com"
    # 블로그 플랫폼은 의도적으로 미보존(검색 blocklist 등치 유지).
    assert normalize_domain("someone.tistory.com") == "tistory.com"
