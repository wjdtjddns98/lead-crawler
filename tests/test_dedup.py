"""dedup canonical_key / 정규화 테스트."""

from __future__ import annotations

from leadcrawler.dedup import canonical_key, normalize_domain, normalize_name


def test_registry_id_takes_priority() -> None:
    key = canonical_key(registry="edgar", registry_id="0000320193", domain="apple.com")
    assert key == "reg:edgar:0000320193"


def test_domain_used_when_no_registry() -> None:
    assert canonical_key(domain="https://www.Apple.com/ir") == "dom:apple.com"


def test_name_country_fallback() -> None:
    assert canonical_key(name="삼성전자 주식회사", country="KR") == "name:kr:삼성전자"


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
