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


def test_missing_identity_raises() -> None:
    try:
        canonical_key()
    except ValueError:
        return
    raise AssertionError("식별 정보가 없으면 ValueError 여야 한다")
