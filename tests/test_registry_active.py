"""등록처 active 체커 테스트 — 주입형 FakeFetcher 로 네트워크 없이 분기 검증."""

from __future__ import annotations

from typing import Any

from leadcrawler.config import Settings
from leadcrawler.verify.registry_active import RegistryActiveChecker, build_registry_checker


class FakeFetcher:
    """SupportsFetch 더블 — get_json 만 라우팅(체커는 GET 만 사용)."""

    def __init__(self, json) -> None:
        self._json = json

    def get_json(self, url: str, *, params=None, headers=None) -> Any:
        return self._json(url, params or {}, headers or {})

    def get_bytes(self, url, *, params=None, headers=None):  # noqa: D102
        raise AssertionError("미사용")

    def get_text(self, url, *, params=None, headers=None):  # noqa: D102
        raise AssertionError("미사용")

    def post_json(self, url, *, json=None, params=None, headers=None):  # noqa: D102
        raise AssertionError("미사용")

    def post_text(self, url, *, data=None, params=None, headers=None):  # noqa: D102
        raise AssertionError("미사용")


def _checker(json, **over) -> RegistryActiveChecker:
    kwargs = {"dry_run": False, "companies_house_api_key": "k", "opencorporates_api_key": "t"}
    kwargs.update(over)
    return RegistryActiveChecker(Settings(**kwargs), fetcher=FakeFetcher(json))


# --- Companies House ----------------------------------------------------

def test_companies_house_active_true() -> None:
    c = _checker(lambda u, p, h: {"company_status": "active"})
    assert c.is_active("companies_house", "01234567") is True


def test_companies_house_dissolved_false() -> None:
    c = _checker(lambda u, p, h: {"company_status": "dissolved"})
    assert c.is_active("companies_house", "01234567") is False


def test_companies_house_unknown_status_none() -> None:
    c = _checker(lambda u, p, h: {"company_status": "open-mystery"})
    assert c.is_active("companies_house", "01234567") is None


def test_companies_house_closed_is_defunct() -> None:
    c = _checker(lambda u, p, h: {"company_status": "closed"})
    assert c.is_active("companies_house", "01234567") is False


def test_companies_house_registered_and_open_are_active() -> None:
    for status in ("registered", "open"):
        c = _checker(lambda u, p, h, s=status: {"company_status": s})
        assert c.is_active("companies_house", "01234567") is True


def test_companies_house_unsafe_number_none() -> None:
    # 경로 변형 차단 — 슬래시/쿼리문자 포함 식별자는 룩업 미수행(None).
    c = _checker(lambda u, p, h: {"company_status": "active"})
    assert c.is_active("companies_house", "01234567?x=1") is None
    assert c.is_active("companies_house", "00/12") is None


def test_companies_house_sends_basic_auth() -> None:
    seen: dict = {}

    def _json(u: str, p: dict, h: dict) -> Any:
        seen.update(h)
        return {"company_status": "active"}

    _checker(_json).is_active("companies_house", "01234567")
    assert seen.get("Authorization", "").startswith("Basic ")


def test_companies_house_no_key_none() -> None:
    c = _checker(lambda u, p, h: {"company_status": "active"}, companies_house_api_key="")
    assert c.is_active("companies_house", "01234567") is None


# --- OpenCorporates -----------------------------------------------------

def test_opencorporates_active_true() -> None:
    c = _checker(lambda u, p, h: {"results": {"company": {"inactive": False}}})
    assert c.is_active("opencorporates", "kr/12345") is True


def test_opencorporates_inactive_false() -> None:
    c = _checker(lambda u, p, h: {"results": {"company": {"inactive": True}}})
    assert c.is_active("opencorporates", "kr/12345") is False


def test_opencorporates_null_status_none() -> None:
    c = _checker(lambda u, p, h: {"results": {"company": {"inactive": None}}})
    assert c.is_active("opencorporates", "kr/12345") is None


def test_opencorporates_bad_registry_id_none() -> None:
    c = _checker(lambda u, p, h: {"results": {"company": {"inactive": False}}})
    assert c.is_active("opencorporates", "no-slash") is None


def test_opencorporates_truthy_nonbool_inactive_none() -> None:
    # inactive 가 bool 아닌 truthy/falsy(문자열·숫자)면 식별불가 → None(절대 False 금지).
    c = _checker(lambda u, p, h: {"results": {"company": {"inactive": "true"}}})
    assert c.is_active("opencorporates", "kr/12345") is None
    c2 = _checker(lambda u, p, h: {"results": {"company": {"inactive": 0}}})
    assert c2.is_active("opencorporates", "kr/12345") is None


def test_opencorporates_extra_slash_in_number_none() -> None:
    # registry_id 에 추가 슬래시(오분할 위험)면 룩업 미수행(None).
    c = _checker(lambda u, p, h: {"results": {"company": {"inactive": False}}})
    assert c.is_active("opencorporates", "kr/12/34") is None


# --- 계약: 오류·미지원·빈 입력은 반드시 None(절대 False 금지) ----------

def test_lookup_error_returns_none_not_false() -> None:
    def _boom(u, p, h):
        raise RuntimeError("network")

    c = _checker(_boom)
    assert c.is_active("companies_house", "01234567") is None
    assert c.is_active("opencorporates", "kr/1") is None


def test_unsupported_registry_none() -> None:
    c = _checker(lambda u, p, h: {"company_status": "active"})
    assert c.is_active("lei", "X") is None
    assert c.is_active("dart", "001") is None


def test_empty_inputs_none() -> None:
    c = _checker(lambda u, p, h: {})
    assert c.is_active(None, "1") is None
    assert c.is_active("companies_house", None) is None


def test_non_dict_payload_none() -> None:
    c = _checker(lambda u, p, h: "boom")
    assert c.is_active("companies_house", "1") is None
    assert c.is_active("opencorporates", "kr/1") is None


# --- 팩토리 -------------------------------------------------------------

def test_factory_returns_none_without_keys() -> None:
    assert build_registry_checker(Settings(dry_run=False)) is None


def test_factory_returns_checker_with_any_key() -> None:
    s = Settings(dry_run=False, opencorporates_api_key="t")
    assert isinstance(build_registry_checker(s), RegistryActiveChecker)
